// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Preferences window backend — Tauri commands + local helpers.
//!
//! The Preferences UI lives in the companion's Svelte app; this module
//! exposes the commands it invokes:
//!
//!   - `get_prefs_state`  — full initial snapshot (Device / Agent /
//!     Network / Advanced). Called once on window open.
//!   - `poll_status`      — cheaper subset for periodic refresh
//!     (status, uptime, transport). Called on a timer while the window
//!     is visible.
//!   - `set_run_at_login` — rewrites `RunAtLoad` on the runtime + companion
//!     LaunchAgent plists via PlistBuddy. Mirrors what the Setup
//!     Assistant does on Finish.
//!   - `runtime_start` / `runtime_stop` / `runtime_restart` — user
//!     controls that shell out to `launchctl` against
//!     `gui/$UID/uk.co.locai.link.agent`.
//!   - `reveal_log_file`  — `open -R /Library/Locai/logs/agent.stdout.log`.
//!   - `open_control_device` — Deep-link to the device's page in
//!     Control (opens in the system browser via `tauri-plugin-opener`).
//!   - `launch_uninstaller_prefs` — the same AppleScript confirm +
//!     admin escalation flow as the (now removed) tray "Uninstall"
//!     item, exposed to the Advanced panel.

use std::path::PathBuf;

use locai_link_shared::{agent_health, HealthStatus, TransportHealth, DEFAULT_HEALTH_URL};
use serde::Serialize;
use tauri::AppHandle;
use tauri_plugin_opener::OpenerExt;

/// Install root laid down by the .pkg. Mirrored in the Setup Assistant
/// (`DEFAULT_INSTALL_ROOT`) and the LaunchAgent plists — a change here
/// has to travel to both.
const INSTALL_ROOT: &str = "/Library/Locai";

/// Control base URL. Same value as `CONTROL_URL` in `lib.rs`; a
/// user-facing deep-link so `openhttps://…/devices/<id>` works.
const CONTROL_BASE_URL: &str = "https://control.locai.co.uk";

/// Path the LaunchAgent plists write stdout to. Reveal opens Finder
/// with this file selected.
const RUNTIME_LOG_FILE: &str = "/Library/Locai/logs/agent.stdout.log";

/// LaunchAgent labels — same as in `bundling/pkg/LaunchAgents/`. The
/// `runtime` label owns the agent process; the `companion` label owns
/// this UI process. macOS-only (only `launchctl` / PlistBuddy code
/// paths use them).
#[cfg(target_os = "macos")]
const AGENT_LABEL: &str = "uk.co.locai.link.agent";
#[cfg(target_os = "macos")]
const COMPANION_LABEL: &str = "uk.co.locai.link.companion";

// --- Command outputs ---------------------------------------------------------

#[derive(Serialize)]
pub struct PrefsState {
    device: Option<DeviceInfo>,
    agent: AgentInfo,
    network: Option<TransportHealth>,
    advanced: AdvancedInfo,
}

#[derive(Serialize)]
struct DeviceInfo {
    name: String,
    id: String,
    /// Deep-link into Control's device page. Empty when no device_id
    /// is known (e.g. runtime never ran; session file missing).
    control_device_url: String,
}

#[derive(Serialize)]
struct AgentInfo {
    status: AgentStatus,
    /// Populated when `status == Up`; `None` otherwise.
    uptime_seconds: Option<u64>,
    /// Runtime version reported via `/healthz`. `None` when Down or
    /// the runtime hasn't wired version resolution.
    version: Option<String>,
    /// `true` when the runtime plist has `RunAtLoad=true`. `false` when
    /// the plist is missing, unreadable, or explicitly `RunAtLoad=false`.
    /// The window's toggle binds to this.
    run_at_login: bool,
}

#[derive(Serialize)]
#[serde(rename_all = "lowercase")]
enum AgentStatus {
    Up,
    Down,
}

#[derive(Serialize)]
struct AdvancedInfo {
    log_file: String,
    install_root: String,
}

/// Trimmed shape returned by the polling command — the fields that
/// change on a running system. Everything else in `PrefsState` is
/// effectively static for the window's lifetime and doesn't need to
/// be re-shipped every tick.
#[derive(Serialize)]
pub struct StatusPoll {
    status: AgentStatus,
    uptime_seconds: Option<u64>,
    version: Option<String>,
    network: Option<TransportHealth>,
}

// --- Commands ----------------------------------------------------------------

#[tauri::command]
pub fn get_prefs_state() -> PrefsState {
    let (agent_status, uptime, version, transport) = probe_runtime();

    let device = read_session_config_device();
    // Version resolution priority: /healthz (source of truth for the
    // running runtime) → `current` symlink (works when runtime is down).
    let effective_version = version.clone().or_else(resolve_current_version);

    PrefsState {
        device,
        agent: AgentInfo {
            status: agent_status,
            uptime_seconds: uptime,
            version: effective_version,
            run_at_login: read_run_at_login(),
        },
        network: transport,
        advanced: AdvancedInfo {
            log_file: RUNTIME_LOG_FILE.to_string(),
            install_root: INSTALL_ROOT.to_string(),
        },
    }
}

#[tauri::command]
pub fn poll_status() -> StatusPoll {
    let (status, uptime, version, network) = probe_runtime();
    StatusPoll {
        status,
        uptime_seconds: uptime,
        version,
        network,
    }
}

#[tauri::command]
#[cfg(target_os = "macos")]
pub fn set_run_at_login(enabled: bool) -> Result<(), String> {
    let plist = user_launchagent_plist(AGENT_LABEL)?;
    let companion_plist = user_launchagent_plist(COMPANION_LABEL)?;
    // Runtime plist controls the agent auto-start; companion plist
    // controls the tray auto-start. Kept in lockstep — if the user
    // wants the tray back at login, they want the runtime too.
    plistbuddy_set_run_at_load(&plist, enabled)?;
    plistbuddy_set_run_at_load(&companion_plist, enabled)?;
    Ok(())
}

#[tauri::command]
#[cfg(not(target_os = "macos"))]
pub fn set_run_at_login(_enabled: bool) -> Result<(), String> {
    Err("set_run_at_login is macOS-only".to_string())
}

#[tauri::command]
#[cfg(target_os = "macos")]
pub fn runtime_start() -> Result<(), String> {
    launchctl(&["kickstart", &agent_service()?])
}

#[tauri::command]
#[cfg(not(target_os = "macos"))]
pub fn runtime_start() -> Result<(), String> {
    Err("runtime_start is macOS-only".to_string())
}

#[tauri::command]
#[cfg(target_os = "macos")]
pub fn runtime_stop() -> Result<(), String> {
    // `kill SIGTERM` stops the current run but leaves the LaunchAgent
    // bootstrapped. If RunAtLoad is true this comes back at next
    // login; if false it stays down until re-kickstarted.
    launchctl(&["kill", "SIGTERM", &agent_service()?])
}

#[tauri::command]
#[cfg(not(target_os = "macos"))]
pub fn runtime_stop() -> Result<(), String> {
    Err("runtime_stop is macOS-only".to_string())
}

#[tauri::command]
#[cfg(target_os = "macos")]
pub fn runtime_restart() -> Result<(), String> {
    // -k forces a restart if already running, otherwise starts fresh.
    launchctl(&["kickstart", "-k", &agent_service()?])
}

#[tauri::command]
#[cfg(not(target_os = "macos"))]
pub fn runtime_restart() -> Result<(), String> {
    Err("runtime_restart is macOS-only".to_string())
}

#[tauri::command]
pub fn reveal_log_file(app: AppHandle) -> Result<(), String> {
    // -R "reveals" (selects) the file in Finder rather than opening
    // it in the default viewer. `open -R` on macOS; `xdg-open` on
    // Linux would open the containing dir but not select — good
    // enough for dev use.
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .args(["-R", RUNTIME_LOG_FILE])
            .status()
            .map_err(|e| format!("open -R: {e}"))?;
        return Ok(());
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = app;
        Err("reveal_log_file is macOS-only".to_string())
    }
}

#[tauri::command]
pub fn open_control_device(app: AppHandle, device_id: Option<String>) -> Result<(), String> {
    let url = match device_id {
        Some(id) if !id.is_empty() => format!("{CONTROL_BASE_URL}/devices/{id}"),
        _ => CONTROL_BASE_URL.to_string(),
    };
    app.opener()
        .open_url(&url, None::<&str>)
        .map_err(|e| format!("open_url {url}: {e}"))
}

/// Ask the user to confirm and, on approval, run the uninstall script
/// with admin privileges via AppleScript. Mirrors what the (deleted)
/// tray menu item used to do — the flow now lives only here, in
/// Preferences → Advanced.
#[tauri::command]
#[cfg(target_os = "macos")]
pub fn launch_uninstaller_prefs() -> Result<(), String> {
    // Detached so the Tauri command returns immediately — the shell
    // script will terminate the companion mid-run when it boots out
    // the LaunchAgent.
    std::thread::spawn(|| {
        let script = "\
try
    set answer to display dialog \"This will remove Locai Link and stop all background services. Your device will remain registered in Control.\" buttons {\"Cancel\", \"Uninstall\"} default button \"Cancel\" with icon caution with title \"Uninstall Locai Link\"
    if button returned of answer is \"Uninstall\" then
        do shell script \"/Library/Locai/uninstall.sh\" with administrator privileges
    end if
end try
";
        let _ = std::process::Command::new("osascript")
            .args(["-e", script])
            .output();
    });
    Ok(())
}

#[tauri::command]
#[cfg(not(target_os = "macos"))]
pub fn launch_uninstaller_prefs() -> Result<(), String> {
    Err("launch_uninstaller_prefs is macOS-only".to_string())
}

// --- Helpers -----------------------------------------------------------------

/// Hit `/healthz` once and unpack the four fields the Preferences UI
/// cares about. Failures collapse to `(Down, None, None, None)` — the
/// UI treats every non-Up response as "agent unreachable".
fn probe_runtime() -> (AgentStatus, Option<u64>, Option<String>, Option<TransportHealth>) {
    match agent_health(DEFAULT_HEALTH_URL) {
        HealthStatus::Up(h) => (
            AgentStatus::Up,
            Some(h.uptime_seconds),
            Some(h.version),
            h.transport,
        ),
        HealthStatus::Down | HealthStatus::Malformed(_) => (AgentStatus::Down, None, None, None),
    }
}

/// Find the newest `session_*.json` under `<install_root>/configs/`
/// and pull the device identity block out of it. Returns `None` if the
/// dir doesn't exist (fresh box, SA hasn't run yet), no session file
/// is present, or the JSON parses but has no `identity` block.
///
/// Files are 0600 owned by the console user; the companion runs as
/// that user, so no privilege elevation is needed.
fn read_session_config_device() -> Option<DeviceInfo> {
    let configs = PathBuf::from(INSTALL_ROOT).join("configs");
    let mut newest: Option<(std::time::SystemTime, PathBuf)> = None;
    for entry in std::fs::read_dir(&configs).ok()?.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if !name.starts_with("session_") || !name.ends_with(".json") {
            continue;
        }
        let mtime = entry
            .metadata()
            .ok()
            .and_then(|m| m.modified().ok())
            .unwrap_or(std::time::UNIX_EPOCH);
        if newest.as_ref().map_or(true, |(t, _)| mtime > *t) {
            newest = Some((mtime, entry.path()));
        }
    }
    let (_, path) = newest?;
    let body = std::fs::read_to_string(&path).ok()?;
    let json: serde_json::Value = serde_json::from_str(&body).ok()?;
    let identity = json.get("identity")?;
    let id = identity.get("device_id")?.as_str()?.to_string();
    let name = identity
        .get("device_name")
        .and_then(|v| v.as_str())
        .unwrap_or(&id)
        .to_string();
    Some(DeviceInfo {
        control_device_url: format!("{CONTROL_BASE_URL}/devices/{id}"),
        id,
        name,
    })
}

/// Resolve `<install_root>/current` and pull the version out of the
/// versioned dir name it points at (`versions/v1.0.19` → `v1.0.19`).
/// Falls back to `None` when the symlink is absent or doesn't follow
/// the expected shape.
fn resolve_current_version() -> Option<String> {
    let current = PathBuf::from(INSTALL_ROOT).join("current");
    let target = std::fs::read_link(&current).ok()?;
    // read_link returns whatever the symlink stores — could be
    // relative (versions/v1.0.19) or absolute. Either way, the last
    // component is the version.
    let last = target.file_name()?.to_string_lossy().into_owned();
    if last.is_empty() {
        return None;
    }
    Some(last)
}

/// Check the runtime LaunchAgent's `RunAtLoad` via PlistBuddy. False
/// on any parse/IO error — a missing plist means "no auto-start" from
/// the user's perspective.
#[cfg(target_os = "macos")]
fn read_run_at_login() -> bool {
    let Ok(plist) = user_launchagent_plist(AGENT_LABEL) else {
        return false;
    };
    let Ok(output) = std::process::Command::new("/usr/libexec/PlistBuddy")
        .args(["-c", "Print :RunAtLoad", plist.to_str().unwrap_or("")])
        .output()
    else {
        return false;
    };
    if !output.status.success() {
        return false;
    }
    let out = String::from_utf8_lossy(&output.stdout);
    out.trim().eq_ignore_ascii_case("true")
}

#[cfg(not(target_os = "macos"))]
fn read_run_at_login() -> bool {
    false
}

#[cfg(target_os = "macos")]
fn user_launchagent_plist(label: &str) -> Result<PathBuf, String> {
    let home = std::env::var("HOME").map_err(|_| "$HOME not set".to_string())?;
    Ok(PathBuf::from(home)
        .join("Library")
        .join("LaunchAgents")
        .join(format!("{label}.plist")))
}

#[cfg(target_os = "macos")]
fn plistbuddy_set_run_at_load(plist: &PathBuf, value: bool) -> Result<(), String> {
    if !plist.exists() {
        // Preferences opened before the SA has ever run — plist isn't
        // laid down yet. Silently succeed rather than surface a scary
        // error; SA will pick up the toggle on Finish.
        return Ok(());
    }
    let val_str = if value { "true" } else { "false" };
    let status = std::process::Command::new("/usr/libexec/PlistBuddy")
        .args([
            "-c",
            &format!("Set :RunAtLoad {val_str}"),
            plist.to_str().unwrap_or(""),
        ])
        .status()
        .map_err(|e| format!("PlistBuddy: {e}"))?;
    if !status.success() {
        return Err(format!(
            "PlistBuddy Set :RunAtLoad {val_str} on {} failed",
            plist.display()
        ));
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn agent_service() -> Result<String, String> {
    let uid = current_uid()?;
    Ok(format!("gui/{uid}/{AGENT_LABEL}"))
}

#[cfg(target_os = "macos")]
fn current_uid() -> Result<String, String> {
    let out = std::process::Command::new("id")
        .arg("-u")
        .output()
        .map_err(|e| format!("id -u: {e}"))?;
    if !out.status.success() {
        return Err("id -u returned non-zero".to_string());
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

#[cfg(target_os = "macos")]
fn launchctl(args: &[&str]) -> Result<(), String> {
    let status = std::process::Command::new("launchctl")
        .args(args)
        .status()
        .map_err(|e| format!("launchctl {args:?}: {e}"))?;
    if !status.success() {
        // launchctl returns non-zero for a lot of harmless cases
        // (kill on an already-dead service, etc.). Bubble the numeric
        // code up but don't treat it as a hard error at this layer —
        // callers that care can special-case; the UI just refetches
        // status after every action anyway.
        return Err(format!("launchctl {args:?} exited with {}", status));
    }
    Ok(())
}
