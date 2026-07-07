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

/// Install root, keyed on host OS. Mirrored in the Setup Assistant
/// (`get_install_root` Tauri command) — a change here has to travel
/// to both.
///
/// * **macOS** — `/Library/Locai` (owned by the .pkg postinstall).
/// * **Linux** — `$HOME/.local/share/locai` (XDG_DATA_HOME). Created
///   on demand by the SA on Finish; no root-privileged install step
///   because the AppImage build has no post-install phase.
fn install_root() -> String {
    #[cfg(target_os = "macos")]
    {
        "/Library/Locai".to_string()
    }
    #[cfg(target_os = "linux")]
    {
        let home = std::env::var("HOME").unwrap_or_default();
        format!("{home}/.local/share/locai")
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        String::new()
    }
}

/// Runtime stdout log path. Companion Preferences → Advanced offers a
/// "Reveal in Finder" (macOS) / "Open folder" (Linux) button that
/// points here.
fn runtime_log_file() -> String {
    format!("{}/logs/agent.stdout.log", install_root())
}

/// Control base URL. Same value as `CONTROL_URL` in `lib.rs`; a
/// user-facing deep-link so `openhttps://…/devices/<id>` works.
const CONTROL_BASE_URL: &str = "https://control.locai.co.uk";

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
    /// `std::env::consts::OS` — "macos" | "linux" | "windows" | …
    /// The Svelte UI branches on this to hide service-management
    /// controls (Start/Stop/Restart, Start-at-login, Uninstall) on
    /// platforms where we haven't wired the equivalent of launchd yet.
    platform: String,
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
            log_file: runtime_log_file(),
            install_root: install_root(),
        },
        platform: std::env::consts::OS.to_string(),
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
#[cfg(target_os = "linux")]
pub fn set_run_at_login(enabled: bool) -> Result<(), String> {
    // systemd equivalent of the plist RunAtLoad flip. `enable` adds
    // the unit to graphical-session.target's wants; `disable` removes
    // it. Neither starts/stops the currently-running instance — that's
    // decoupled from the enable state, matching what the user expects
    // when they flip "Start at login" (behaviour at next login).
    for unit in ["locai-link-agent.service", "locai-link-companion.service"] {
        let verb = if enabled { "enable" } else { "disable" };
        systemctl(&["--user", verb, unit])?;
    }
    Ok(())
}

#[tauri::command]
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub fn set_run_at_login(_enabled: bool) -> Result<(), String> {
    Err("set_run_at_login: unsupported platform".to_string())
}

#[tauri::command]
#[cfg(target_os = "macos")]
pub fn runtime_start() -> Result<(), String> {
    launchctl(&["kickstart", &agent_service()?])
}

#[tauri::command]
#[cfg(target_os = "linux")]
pub fn runtime_start() -> Result<(), String> {
    // `start` (not `start --now`) runs the unit now without affecting
    // whether it auto-starts at next login — the enable/disable state
    // is controlled by the "Start at login" toggle via set_run_at_login.
    systemctl(&["--user", "start", "locai-link-agent.service"])
}

#[tauri::command]
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub fn runtime_start() -> Result<(), String> {
    Err("runtime_start: unsupported platform".to_string())
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
#[cfg(target_os = "linux")]
pub fn runtime_stop() -> Result<(), String> {
    // `stop` sends SIGTERM to the running instance; the unit stays
    // enabled (if it was) and would come back at next login. Same
    // semantics as the macOS `launchctl kill SIGTERM` above.
    systemctl(&["--user", "stop", "locai-link-agent.service"])
}

#[tauri::command]
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub fn runtime_stop() -> Result<(), String> {
    Err("runtime_stop: unsupported platform".to_string())
}

#[tauri::command]
#[cfg(target_os = "macos")]
pub fn runtime_restart() -> Result<(), String> {
    // -k forces a restart if already running, otherwise starts fresh.
    launchctl(&["kickstart", "-k", &agent_service()?])
}

#[tauri::command]
#[cfg(target_os = "linux")]
pub fn runtime_restart() -> Result<(), String> {
    // `restart` stops + starts atomically. Starts fresh if the unit
    // wasn't running.
    systemctl(&["--user", "restart", "locai-link-agent.service"])
}

#[tauri::command]
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub fn runtime_restart() -> Result<(), String> {
    Err("runtime_restart: unsupported platform".to_string())
}

#[tauri::command]
pub fn reveal_log_file(app: AppHandle) -> Result<(), String> {
    let _ = app;
    let log = runtime_log_file();
    // macOS: `open -R <file>` reveals the specific file in Finder.
    // Linux: `xdg-open <dir>` opens the containing dir in the user's
    // file manager (no equivalent "select this file" gesture in the
    // XDG spec — good enough for pointing the user at the logs).
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .args(["-R", &log])
            .status()
            .map_err(|e| format!("open -R: {e}"))?;
        Ok(())
    }
    #[cfg(target_os = "linux")]
    {
        let dir = std::path::Path::new(&log)
            .parent()
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_else(install_root);
        // Make sure the dir exists — on first-run Linux, the runtime
        // may not have started yet and `logs/` won't be there.
        let _ = std::fs::create_dir_all(&dir);
        std::process::Command::new("xdg-open")
            .arg(&dir)
            .status()
            .map_err(|e| format!("xdg-open: {e}"))?;
        Ok(())
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        Err("reveal_log_file: unsupported platform".to_string())
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

/// Launch `~/.local/share/locai/uninstall.sh`. No admin prompt — the
/// per-user install has no root-owned files.
///
/// The script's first act is `systemctl --user disable --now
/// locai-link-companion.service`, which SIGTERMs this very process.
/// Spawning bash as a child of the companion isn't enough: systemd
/// tracks services via cgroups, and disabling the companion kills
/// everything in its cgroup — the uninstall bash included. `setsid` /
/// `nohup` don't help because they move the child to a new session,
/// not a new cgroup.
///
/// The clean fix is `systemd-run --user` — that launches the script
/// as a transient user-scope service with its own cgroup, decoupled
/// from the companion. When the companion dies mid-run, this transient
/// unit keeps going and finishes the uninstall. `--collect` reaps the
/// unit after exit so it doesn't linger as "failed"/"dead".
///
/// We skip the confirm dialog here (macOS gets one via osascript, but
/// Linux would need a per-DE tool like zenity/kdialog). The button
/// label is already "Uninstall Locai Link…" and lives in the
/// destructive-action row; that's enough friction for MVP.
#[tauri::command]
#[cfg(target_os = "linux")]
pub fn launch_uninstaller_prefs() -> Result<(), String> {
    let script = format!("{}/uninstall.sh", install_root());
    if !std::path::Path::new(&script).exists() {
        return Err(format!("uninstall.sh not found at {script}"));
    }
    let out = std::process::Command::new("systemd-run")
        .args([
            "--user",
            "--collect",
            "--description=Locai Link uninstaller",
            "--",
            "/bin/bash",
            &script,
        ])
        .output()
        .map_err(|e| format!("systemd-run: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "systemd-run failed ({:?}): {}",
            out.status.code(),
            String::from_utf8_lossy(&out.stderr).trim(),
        ));
    }
    Ok(())
}

#[tauri::command]
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub fn launch_uninstaller_prefs() -> Result<(), String> {
    Err("launch_uninstaller_prefs: unsupported platform".to_string())
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
    let configs = PathBuf::from(install_root()).join("configs");
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
    let current = PathBuf::from(install_root()).join("current");
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

/// Linux equivalent of the plist `Print :RunAtLoad` query. Consults
/// `systemctl --user is-enabled locai-link-agent.service`; that returns
/// exit-code 0 with stdout "enabled" iff the unit is set to auto-start
/// at next login. Anything else (disabled, static, not-found) → false.
#[cfg(target_os = "linux")]
fn read_run_at_login() -> bool {
    let Ok(output) = std::process::Command::new("systemctl")
        .args(["--user", "is-enabled", "locai-link-agent.service"])
        .output()
    else {
        return false;
    };
    if !output.status.success() {
        return false;
    }
    let out = String::from_utf8_lossy(&output.stdout);
    out.trim().eq_ignore_ascii_case("enabled")
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
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

/// Linux twin of `launchctl`. Same "capture-and-bubble" semantics —
/// `systemctl` also returns non-zero for "not enabled" / "not running"
/// which callers may want to treat as info rather than error, so we
/// surface the exit code without editorialising.
#[cfg(target_os = "linux")]
fn systemctl(args: &[&str]) -> Result<(), String> {
    let out = std::process::Command::new("systemctl")
        .args(args)
        .output()
        .map_err(|e| format!("systemctl {args:?}: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "systemctl {args:?} exited {:?}: {}",
            out.status.code(),
            String::from_utf8_lossy(&out.stderr).trim(),
        ));
    }
    Ok(())
}
