// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Preferences window backend — Tauri commands invoked by the companion's
//! Svelte UI. Uninstall lives in the Setup Assistant, not here.

use std::path::PathBuf;

use locai_link_shared::{
    agent_health, cancel_deployment as shared_cancel_deployment,
    list_available_models as shared_list_available_models, list_models, mark_deployment_pending,
    read_identity, request_deploy as shared_request_deploy,
    toggle_serving as shared_toggle_serving, trigger_update, AvailableModel, DeployOutcome,
    DeploymentProgress, HealthStatus, ModelInfo, ModelsStatus, ServingAction, TransportHealth,
    DEFAULT_HEALTH_URL, DEFAULT_MODELS_URL, DEFAULT_MODEL_ACTION_BASE, DEFAULT_PENDING_URL,
    DEFAULT_UPDATE_URL,
};
use serde::Serialize;
use tauri::AppHandle;
use tauri_plugin_opener::OpenerExt;

/// Install root. Mirrored in the Setup Assistant's `get_install_root` —
/// a change here has to travel to both.
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

fn runtime_log_file() -> String {
    format!("{}/logs/agent.stdout.log", install_root())
}

const CONTROL_BASE_URL: &str = "https://control.locai.co.uk";

/// LaunchAgent labels — must match `bundling/pkg/LaunchAgents/`.
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
    /// `std::env::consts::OS` — Svelte UI branches on this to hide
    /// service-management controls on unsupported platforms.
    platform: String,
}

#[derive(Serialize)]
struct DeviceInfo {
    name: String,
    id: String,
    /// Deep-link into Control. Empty when no device_id is known.
    control_device_url: String,
}

#[derive(Serialize)]
struct AgentInfo {
    status: AgentStatus,
    uptime_seconds: Option<u64>,
    version: Option<String>,
    /// Runtime plist `RunAtLoad` — false when the plist is missing/unreadable.
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

/// Trimmed poll shape — omits the static fields in `PrefsState` that
/// don't change over the window's lifetime.
#[derive(Serialize)]
pub struct StatusPoll {
    status: AgentStatus,
    uptime_seconds: Option<u64>,
    version: Option<String>,
    network: Option<TransportHealth>,
    /// Empty on transient failures — UI treats that as "no models yet".
    models: Vec<ModelInfo>,
    deployments: Vec<DeploymentProgress>,
    /// A newer bundle is published for this platform (INFRA-353).
    update_available: bool,
    latest_version: Option<String>,
}

// --- Commands ----------------------------------------------------------------

// get_prefs_state does file-only reads — no HTTP. Dynamic fields (agent status,
// uptime, network, models) come from poll_status right after mount. Kept on
// spawn_blocking anyway because read_session_config_device/read_run_at_login
// hit the disk and shouldn't stall the WebView thread.
#[tauri::command]
pub async fn get_prefs_state() -> PrefsState {
    tauri::async_runtime::spawn_blocking(|| {
        let device = read_session_config_device();
        PrefsState {
            device,
            agent: AgentInfo {
                status: AgentStatus::Down,
                uptime_seconds: None,
                version: resolve_current_version(),
                run_at_login: read_run_at_login(),
            },
            network: None,
            advanced: AdvancedInfo {
                log_file: runtime_log_file(),
                install_root: install_root(),
            },
            platform: std::env::consts::OS.to_string(),
        }
    })
    .await
    .expect("prefs probe panicked")
}

#[tauri::command]
pub async fn poll_status() -> StatusPoll {
    tauri::async_runtime::spawn_blocking(|| {
        let probe = probe_runtime_full();
        let models = match list_models(DEFAULT_MODELS_URL) {
            ModelsStatus::Ok(list) => list,
            ModelsStatus::Down | ModelsStatus::Malformed(_) => Vec::new(),
        };
        StatusPoll {
            status: probe.status,
            uptime_seconds: probe.uptime_seconds,
            version: probe.version,
            network: probe.network,
            models,
            deployments: probe.deployments,
            update_available: probe.update_available,
            latest_version: probe.latest_version,
        }
    })
    .await
    .expect("poll_status panicked")
}

/// Trigger the agent's in-app update (INFRA-353). POSTs the loopback
/// `/update`; the agent swaps the bundle and relaunches on success.
#[tauri::command]
pub fn install_update() -> Result<(), String> {
    trigger_update(DEFAULT_UPDATE_URL)
}

/// Start or stop serving `pipeline_id`. `action` is `"serve"` or `"stop-serving"`.
#[tauri::command]
pub fn toggle_model_serving(pipeline_id: String, action: String) -> Result<(), String> {
    let parsed = match action.as_str() {
        "serve" => ServingAction::Start,
        "stop-serving" => ServingAction::Stop,
        other => return Err(format!("unknown action: {other}")),
    };
    shared_toggle_serving(DEFAULT_MODEL_ACTION_BASE, &pipeline_id, parsed)
}

/// Cancel an in-flight deploy for `pipeline_id`. Runtime is idempotent — a
/// cancel with no active worker returns success with a note.
#[tauri::command]
pub fn cancel_model_deploy(pipeline_id: String) -> Result<(), String> {
    shared_cancel_deployment(DEFAULT_MODEL_ACTION_BASE, &pipeline_id)
}

/// List the models this device may install, from Control's device-authenticated
/// `available-models` endpoint (INFRA-343). Reads the device key + api_url from
/// the session config (no user token). `Err` carries a display string so the UI
/// can show why the list is unavailable (offline, not-yet-claimed device, etc.).
#[tauri::command]
pub async fn list_available_models() -> Result<Vec<AvailableModel>, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let identity = read_identity(&PathBuf::from(install_root()))
            .ok_or_else(|| "device not registered yet".to_string())?;
        shared_list_available_models(&identity)
    })
    .await
    .map_err(|e| format!("list_available_models task panicked: {e}"))?
}

/// Ask Control to deploy `model_id` onto this device (INFRA-343). Idempotent on
/// Control's side. On a dispatched/pending outcome, pre-registers a queued row
/// with the local agent so the download shows at 0% immediately. `model_name`
/// (the asset filename) labels that row until the runtime reports real progress.
#[tauri::command]
pub async fn request_model_deploy(
    model_id: String,
    model_name: Option<String>,
) -> Result<DeployOutcome, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let identity = read_identity(&PathBuf::from(install_root()))
            .ok_or_else(|| "device not registered yet".to_string())?;
        let outcome = shared_request_deploy(&identity, &model_id)?;
        // Only pre-register when a deploy is actually happening; `already_installed`
        // has nothing to show. Best-effort; a failed pre-register just means the
        // row appears a beat later when the runtime reports progress.
        if outcome.status == "dispatched" || outcome.status == "pending" {
            if let Err(e) =
                mark_deployment_pending(DEFAULT_PENDING_URL, &model_id, model_name.as_deref())
            {
                eprintln!("[companion] mark_deployment_pending({model_id}) failed: {e}");
            }
        }
        Ok(outcome)
    })
    .await
    .map_err(|e| format!("request_model_deploy task panicked: {e}"))?
}

#[tauri::command]
#[cfg(target_os = "macos")]
pub fn set_run_at_login(enabled: bool) -> Result<(), String> {
    let plist = user_launchagent_plist(AGENT_LABEL)?;
    let companion_plist = user_launchagent_plist(COMPANION_LABEL)?;
    // Runtime + companion move in lockstep.
    plistbuddy_set_run_at_load(&plist, enabled)?;
    plistbuddy_set_run_at_load(&companion_plist, enabled)?;
    Ok(())
}

#[tauri::command]
#[cfg(target_os = "linux")]
pub fn set_run_at_login(enabled: bool) -> Result<(), String> {
    // enable/disable governs next-login behaviour only; the running
    // instance is untouched — matches user expectations for this toggle.
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
    // Stops the current run; LaunchAgent bootstrap + RunAtLoad are untouched.
    launchctl(&["kill", "SIGTERM", &agent_service()?])
}

#[tauri::command]
#[cfg(target_os = "linux")]
pub fn runtime_stop() -> Result<(), String> {
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
    // Linux has no XDG equivalent of `open -R` to reveal a specific file —
    // just open the containing dir.
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
        // First-run Linux: runtime hasn't created logs/ yet.
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

// --- Helpers -----------------------------------------------------------------

/// Full /healthz probe including in-flight deployments; used by `poll_status`.
struct RuntimeProbe {
    status: AgentStatus,
    uptime_seconds: Option<u64>,
    version: Option<String>,
    network: Option<TransportHealth>,
    deployments: Vec<DeploymentProgress>,
    update_available: bool,
    latest_version: Option<String>,
}

fn probe_runtime_full() -> RuntimeProbe {
    match agent_health(DEFAULT_HEALTH_URL) {
        HealthStatus::Up(h) => RuntimeProbe {
            status: AgentStatus::Up,
            uptime_seconds: Some(h.uptime_seconds),
            version: Some(h.version),
            network: h.transport,
            deployments: h.deployments,
            update_available: h.update_available,
            latest_version: h.latest_version,
        },
        HealthStatus::Down | HealthStatus::Malformed(_) => RuntimeProbe {
            status: AgentStatus::Down,
            uptime_seconds: None,
            version: None,
            network: None,
            deployments: Vec::new(),
            update_available: false,
            latest_version: None,
        },
    }
}

/// Pull the identity block from the newest `session_*.json` under `configs/`.
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
        if newest.as_ref().is_none_or(|(t, _)| mtime > *t) {
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

/// Resolve `<install_root>/current` and return the final path component
/// (the version dir name), or `None` when the symlink is absent or unusable.
fn resolve_current_version() -> Option<String> {
    let current = PathBuf::from(install_root()).join("current");
    let target = std::fs::read_link(&current).ok()?;
    let last = target.file_name()?.to_string_lossy().into_owned();
    if last.is_empty() {
        return None;
    }
    Some(last)
}

/// Check the runtime LaunchAgent's `RunAtLoad` via PlistBuddy. False on any
/// parse/IO error — missing plist means "no auto-start".
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

/// Linux equivalent of the plist RunAtLoad query — `systemctl is-enabled`.
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
        // Preferences opened before SA has ever run — silently succeed;
        // SA will pick up the toggle on Finish.
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
        // launchctl returns non-zero for harmless cases (kill on already-dead
        // service, etc.); bubble the code — callers can special-case.
        return Err(format!("launchctl {args:?} exited with {}", status));
    }
    Ok(())
}

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
