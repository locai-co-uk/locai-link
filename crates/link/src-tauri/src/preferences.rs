// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Preferences window backend — Tauri commands invoked by the app's
//! Svelte UI. Uninstall lives in the Setup Assistant, not here.

use std::path::PathBuf;

use crate::shared::{
    agent_health, cancel_deployment as shared_cancel_deployment, installed_version,
    list_available_models as shared_list_available_models, list_models, mark_deployment_pending,
    read_identity, read_session_identity, request_deploy as shared_request_deploy,
    supported_model_types as shared_supported_model_types, toggle_serving as shared_toggle_serving,
    trigger_update, uninstall_model as shared_uninstall_model, AvailableModel, DeployOutcome,
    DeploymentProgress, HealthStatus, ModelInfo, ModelsStatus, ServingAction, TransportHealth,
    DEFAULT_HEALTH_URL, DEFAULT_MODELS_URL, DEFAULT_MODEL_ACTION_BASE, DEFAULT_PENDING_URL,
    DEFAULT_UPDATE_URL,
};
use serde::Serialize;
use tauri::AppHandle;
use tauri_plugin_opener::OpenerExt;

/// Install root — single source in the shared crate.
pub(crate) fn install_root() -> String {
    crate::shared::install_root()
}

fn runtime_log_file() -> String {
    format!("{}/logs/link.stdout.log", install_root())
}

const CONTROL_BASE_URL: &str = crate::shared::CONTROL_URL;

/// LaunchAgent label; value single-sourced in shared (must match `bundling/pkg/LaunchAgents/`).
#[cfg(target_os = "macos")]
const COMPANION_LABEL: &str = crate::shared::COMPANION_APP_ID;

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
    /// A newer bundle is published for this platform.
    update_available: bool,
    latest_version: Option<String>,
    /// An OTA swap is applying: the UI locks and shows "updating, will restart"
    /// until the app relaunches on the new build.
    update_in_flight: bool,
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
pub async fn poll_status(
    handles: tauri::State<'_, crate::tray::SharedHandles>,
) -> Result<StatusPoll, ()> {
    // Read the shared flag before the await so no State reference is held across
    // it. Fail closed: if the lock is poisoned, assume an update is in flight so
    // the UI stays locked rather than re-enabling actions mid-swap.
    let update_in_flight = handles.lock().map(|h| h.update_in_flight).unwrap_or(true);
    let poll = tauri::async_runtime::spawn_blocking(move || {
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
            update_in_flight,
        }
    })
    .await
    .expect("poll_status panicked");
    Ok(poll)
}

/// Trigger the agent's in-app update. POSTs the loopback
/// `/update`; the agent swaps the bundle and relaunches on success.
/// Sets `update_in_flight` so Preferences locks into the "updating" state; a
/// POST failure clears it (the tray path does the same).
#[tauri::command]
pub fn install_update(
    handles: tauri::State<'_, crate::tray::SharedHandles>,
    control: tauri::State<'_, crate::supervisor::SupervisorControl>,
) -> Result<(), String> {
    // Atomic check-and-set: reject a second trigger while one is in flight, so a
    // double-click can't fire two updates.
    {
        let mut h = handles
            .lock()
            .map_err(|_| "link state lock poisoned".to_string())?;
        if h.update_in_flight {
            return Err("An update is already in progress.".to_string());
        }
        h.update_in_flight = true;
        // Capture the supervisor's restart-for-update epoch; poll_forever clears
        // the lock once it advances (the runtime restarted for the update).
        h.update_restart_epoch_at_trigger = Some(control.update_restart_epoch());
    }
    let res = trigger_update(DEFAULT_UPDATE_URL);
    // The POST is to the local loopback agent and returns 202 before any
    // shutdown, so an error is unambiguous (the update never started): safe to
    // clear the lock and allow a retry. If it succeeds but the update later
    // fails without restarting the agent, the supervisor's restart signal (or
    // the health Up->Down->Up resolution) releases it.
    if res.is_err() {
        if let Ok(mut h) = handles.lock() {
            h.update_in_flight = false;
            h.update_restart_epoch_at_trigger = None;
        }
    }
    res
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

/// Remove `pipeline_id` from this node — deletes the local config and on-disk
/// artifact, stopping the model first if it's serving. On success reports the
/// removal to Control so the dashboard drops it.
#[tauri::command]
pub fn uninstall_model(pipeline_id: String) -> Result<(), String> {
    shared_uninstall_model(DEFAULT_MODEL_ACTION_BASE, &pipeline_id)
}

/// List the models this device may install, from Control's device-authenticated
/// `available-models` endpoint. Reads the device key + api_url from
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

/// Model types this build can actually serve, derived from the current bundle's
/// manifest plugins. The UI filters the model lists to these so
/// an LLM-only build never offers audio/other models it can't run.
#[tauri::command]
pub fn supported_model_types() -> Vec<String> {
    shared_supported_model_types(&PathBuf::from(install_root()))
}

/// Ask Control to deploy `model_id` onto this device. Idempotent on
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
                eprintln!("[link] mark_deployment_pending({model_id}) failed: {e}");
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
    // One unit now: RunAtLoad on the single LaunchAgent plist.
    let plist = user_launchagent_plist(COMPANION_LABEL)?;
    plistbuddy_set_run_at_load(&plist, enabled)
}

#[tauri::command]
#[cfg(target_os = "linux")]
pub fn set_run_at_login(enabled: bool) -> Result<(), String> {
    // enable/disable governs next-login behaviour only; the running instance is
    // untouched. One unit now.
    let verb = if enabled { "enable" } else { "disable" };
    systemctl(&["--user", verb, "locai-link-companion.service"])
}

#[tauri::command]
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub fn set_run_at_login(_enabled: bool) -> Result<(), String> {
    Err("set_run_at_login: unsupported platform".to_string())
}

// Runtime lifecycle now goes through the in-process supervisor (the merged
// binary owns the Python child), not a separate service. Cross-platform.
type Control<'a> = tauri::State<'a, crate::supervisor::SupervisorControl>;

#[tauri::command]
pub fn runtime_start(control: Control<'_>) -> Result<(), String> {
    control.start();
    Ok(())
}

#[tauri::command]
pub fn runtime_stop(control: Control<'_>) -> Result<(), String> {
    control.stop();
    Ok(())
}

#[tauri::command]
pub fn runtime_restart(control: Control<'_>) -> Result<(), String> {
    control.restart();
    Ok(())
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

/// Open the install-root folder in the system file manager.
#[tauri::command]
pub fn open_install_root() -> Result<(), String> {
    let root = install_root();
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(&root)
            .status()
            .map_err(|e| format!("open: {e}"))?;
        Ok(())
    }
    #[cfg(target_os = "linux")]
    {
        std::process::Command::new("xdg-open")
            .arg(&root)
            .status()
            .map_err(|e| format!("xdg-open: {e}"))?;
        Ok(())
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        Err("open_install_root: unsupported platform".to_string())
    }
}

#[tauri::command]
pub fn open_control_device(app: AppHandle, device_id: Option<String>) -> Result<(), String> {
    let url = match device_id {
        Some(id) if !id.is_empty() => {
            format!(
                "{CONTROL_BASE_URL}/devices/{}",
                crate::shared::encode_segment(&id)
            )
        }
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

/// Device info (id, name, Control URL) from the newest session config.
fn read_session_config_device() -> Option<DeviceInfo> {
    let identity = read_session_identity(&PathBuf::from(install_root()))?;
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

/// Resolve `<install_root>/current` to the active version dir name, or `None`
/// when it can't be resolved. Delegates to the shared resolver so it honours
/// BOTH the `current` symlink and the `CURRENT` text pointer — a bare
/// `read_link` here silently returned `None` on text-pointer installs, so the
/// app showed no version where the runtime/SA (which use the shared
/// resolver) showed it.
fn resolve_current_version() -> Option<String> {
    installed_version(&PathBuf::from(install_root())).map(|v| v.version)
}

/// Check the runtime LaunchAgent's `RunAtLoad` via PlistBuddy. False on any
/// parse/IO error — missing plist means "no auto-start".
#[cfg(target_os = "macos")]
fn read_run_at_login() -> bool {
    let Ok(plist) = user_launchagent_plist(COMPANION_LABEL) else {
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
        .args(["--user", "is-enabled", "locai-link-companion.service"])
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
fn plistbuddy_set_run_at_load(plist: &std::path::Path, value: bool) -> Result<(), String> {
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
pub(crate) fn current_uid() -> Result<String, String> {
    let out = std::process::Command::new("id")
        .arg("-u")
        .output()
        .map_err(|e| format!("id -u: {e}"))?;
    if !out.status.success() {
        return Err("id -u returned non-zero".to_string());
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
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
