// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! First-run onboarding backend — Tauri commands driving the setup window's
//! Svelte wizard (device sign-in, register, install config + services, deploy
//! first model). Preferences backend lives in `preferences`.

use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{Manager, State};

use crate::shared::{
    deregister_device, installed_version, read_boot_json, read_identity, read_session_identity,
    BootConfig,
};

// Overridable at build time via `LOCAI_CONTROL_API_URL` (dev builds); unset
// defaults to prod. Registration writes this into the session config, so every
// device-authenticated call (deregister, uninstall-report) follows the same env.
const CONTROL_API_URL: &str = match option_env!("LOCAI_CONTROL_API_URL") {
    Some(url) => url,
    None => "https://api.locai.co.uk/api/v1",
};

/// Legacy agent LaunchAgent label — still referenced to boot out + remove the
/// pre-merge unit on upgrade. Value single-sourced in shared.
#[cfg(target_os = "macos")]
const AGENT_LABEL: &str = crate::shared::AGENT_APP_ID;

/// Generous because the backend hits Firestore synchronously on both paths.
const HTTP_TIMEOUT: Duration = Duration::from_secs(15);

/// Longer per-attempt budget for the device-code request: the first post-boot
/// call can wake a scaled-to-zero backend, and a cold start exceeding
/// HTTP_TIMEOUT surfaced as a sign-in timeout that only cleared on retry.
const DEVICE_CODE_TIMEOUT: Duration = Duration::from_secs(45);

/// The deploy enqueue POST can wake a scaled-to-zero backend, so a cold start
/// exceeding HTTP_TIMEOUT surfaced as a false "deploy failed" even though
/// Control had queued the deploy and the model landed over Zenoh.
const DEPLOY_TIMEOUT: Duration = Duration::from_secs(45);

/// Google-fronted endpoints have been observed to stall on requests without an explicit UA.
const USER_AGENT: &str = "locai-link-setup-assistant/0.1.0";

fn http_agent() -> ureq::Agent {
    http_agent_with_timeout(HTTP_TIMEOUT)
}

fn http_agent_with_timeout(timeout: Duration) -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout(timeout)
        .user_agent(USER_AGENT)
        .build()
}

/// Device name derived from the machine's hostname. Short hostnames get
/// suffixed with the OS name because Control requires `Device.name` >= 3 chars.
fn machine_hostname() -> String {
    let raw = std::process::Command::new("hostname")
        .output()
        .ok()
        .and_then(|out| {
            if !out.status.success() {
                return None;
            }
            let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if s.is_empty() {
                None
            } else {
                Some(s)
            }
        });

    match raw {
        Some(h) if h.chars().count() >= 3 => h,
        // Short but non-empty (e.g. `pc`) — append OS so the machine's still recognisable.
        Some(h) => format!("{h}-{}", std::env::consts::OS),
        None => format!("locai-link-{}", std::env::consts::OS),
    }
}

#[tauri::command]
pub fn suggest_device_name() -> String {
    machine_hostname()
}

// --- Check Install -----------------------------------------------------------

/// Wire-format result for the setup wizard's "Check Install" step.
#[derive(Serialize)]
pub struct CheckInstallResult {
    pub installed: bool,
    pub version: Option<String>,
    pub path: Option<String>,
    pub boot: Option<BootConfig>,
    /// Distinct from `reason` so the UI can say "install found but config is broken".
    pub boot_error: Option<String>,
    pub reason: Option<String>,
    /// From the newest `session_*.json`; when set, the setup UI renders the
    /// "already set up" splash instead of the wizard.
    pub device_id: Option<String>,
    pub device_name: Option<String>,
}

fn resolve_install_root() -> String {
    crate::shared::install_root()
}

/// Install root, keyed on host OS. Mirrored in the app's `install_root`.
#[tauri::command]
pub fn get_install_root() -> String {
    resolve_install_root()
}

/// OS the app is running on, so the frontend can render platform-appropriate strings.
#[tauri::command]
pub fn get_platform() -> String {
    std::env::consts::OS.to_string()
}

/// Read the on-disk install state. Never `Err` — the failure modes are legitimate
/// outcomes the UI needs to render; `reason` / `boot_error` carry the detail.
#[tauri::command]
pub fn check_install(install_root: String) -> CheckInstallResult {
    let root = PathBuf::from(&install_root);
    if !root.exists() {
        return CheckInstallResult {
            installed: false,
            version: None,
            path: None,
            boot: None,
            boot_error: None,
            reason: Some(format!("Install root does not exist: {install_root}")),
            device_id: None,
            device_name: None,
        };
    }

    let boot_path = root.join("boot.json");
    let (boot, boot_error) = if boot_path.exists() {
        match read_boot_json(&boot_path) {
            Ok(cfg) => (Some(cfg), None),
            Err(e) => (None, Some(format!("boot.json unreadable: {e}"))),
        }
    } else {
        (None, None)
    };

    let (device_id, device_name) = read_registered_identity(&root);

    match installed_version(&root) {
        Some(v) => CheckInstallResult {
            installed: true,
            version: Some(v.version),
            path: Some(v.path.to_string_lossy().into_owned()),
            boot,
            boot_error,
            reason: None,
            device_id,
            device_name,
        },
        None => CheckInstallResult {
            installed: false,
            version: None,
            path: None,
            boot,
            boot_error,
            reason: Some("No `current` pointer found under install root.".to_string()),
            device_id,
            device_name,
        },
    }
}

/// `(device_id, device_name)` from the newest session config, best-effort.
fn read_registered_identity(install_root: &Path) -> (Option<String>, Option<String>) {
    let Some(identity) = read_session_identity(install_root) else {
        return (None, None);
    };
    let id = identity
        .get("device_id")
        .and_then(|v| v.as_str())
        .map(String::from);
    let name = identity
        .get("device_name")
        .and_then(|v| v.as_str())
        .map(String::from);
    (id, name)
}

// --- Sign in (RFC 8628 device authorization) --------------------------------

/// Mirrors backend's `DeviceCodeResponse` minus the `device_code` (kept
/// server-side in `SignInState`).
#[derive(Serialize)]
pub struct DeviceCodeStart {
    pub user_code: String,
    pub verification_uri: String,
    pub verification_uri_complete: String,
    pub interval: u64,
    pub expires_in: u64,
}

/// Control's `POST /auth/device` response. Typed so a shape/format change is a
/// deserialize error instead of a silently dropped field.
#[derive(Deserialize)]
struct DeviceCodeResponse {
    device_code: String,
    user_code: String,
    verification_uri: String,
    verification_uri_complete: Option<String>,
    #[serde(default = "default_poll_interval")]
    interval: u64,
    #[serde(default = "default_expires_in")]
    expires_in: u64,
}

fn default_poll_interval() -> u64 {
    5
}

fn default_expires_in() -> u64 {
    600
}

/// Control's `POST /auth/device/token` success response.
#[derive(Deserialize)]
struct TokenResponse {
    #[serde(default)]
    access_token: String,
    refresh_token: Option<String>,
    #[serde(default)]
    user: TokenUser,
}

#[derive(Deserialize, Default)]
struct TokenUser {
    #[serde(default)]
    id: String,
    #[serde(default)]
    email: String,
    #[serde(default)]
    username: String,
}

/// Every RFC §3.5 error case is a distinct variant so the front-end
/// doesn't inspect strings.
#[derive(Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum SignInPollResult {
    Pending,
    SlowDown,
    Approved {
        user_id: String,
        email: String,
        username: String,
    },
    Denied,
    Expired,
    Error {
        message: String,
    },
}

/// Session held server-side — the JWT never crosses the Tauri IPC boundary.
struct Session {
    device_code: String,
    access_token: Option<String>,
    #[allow(dead_code)] // consumed later by register-with-key wiring
    refresh_token: Option<String>,
    user_id: Option<String>,
    email: Option<String>,
}

#[derive(Default)]
pub struct SignInState {
    inner: Mutex<Option<Session>>,
}

/// Kick off RFC 8628 device authorization. Uses DEVICE_CODE_TIMEOUT plus one
/// backed-off retry on transport errors so a cold (scaled-to-zero) backend
/// doesn't surface as a sign-in failure. The retry waits first — an immediate
/// one doesn't help while the instance is still warming.
#[tauri::command]
pub async fn sign_in_start(state: State<'_, SignInState>) -> Result<DeviceCodeStart, String> {
    // HTTP + optional retry sleep runs on the blocking pool so the setup
    // window doesn't freeze during the device-code request.
    let (device_code, start) = tauri::async_runtime::spawn_blocking(move || -> Result<(String, DeviceCodeStart), String> {
        let payload = serde_json::json!({
            "client_metadata": {
                "os": std::env::consts::OS,
                "source": "setup_assistant",
            }
        });
        // One-shot UI call; boxing ureq::Error isn't worth losing the
        // direct Transport-variant match below.
        #[allow(clippy::result_large_err)]
        let send = || {
            http_agent_with_timeout(DEVICE_CODE_TIMEOUT)
                .post(&format!("{CONTROL_API_URL}/auth/device/code"))
                .set("Accept", "application/json")
                .send_json(payload.clone())
        };
        let resp = match send() {
            Ok(r) => r,
            Err(ureq::Error::Transport(t)) => {
                eprintln!("sign_in_start: first attempt failed (transport: {t}); retrying after backoff");
                std::thread::sleep(std::time::Duration::from_secs(4));
                send().map_err(|e| describe_ureq_err("device code request", e))?
            }
            Err(e) => return Err(describe_ureq_err("device code request", e)),
        };

        let body: DeviceCodeResponse = resp
            .into_json()
            .map_err(|e| format!("device code response malformed: {e}"))?;

        let verification_uri_complete = body
            .verification_uri_complete
            .filter(|s| !s.trim().is_empty())
            .unwrap_or_else(|| body.verification_uri.clone());

        Ok((
            body.device_code,
            DeviceCodeStart {
                user_code: body.user_code,
                verification_uri: body.verification_uri,
                verification_uri_complete,
                interval: body.interval,
                expires_in: body.expires_in,
            },
        ))
    })
    .await
    .expect("sign_in_start panicked")?;

    // Persist the device_code into shared state on return — held only
    // during the (fast) mutex critical section, safe from the main thread.
    *state.inner.lock().expect("SignInState poisoned") = Some(Session {
        device_code,
        access_token: None,
        refresh_token: None,
        user_id: None,
        email: None,
    });

    Ok(start)
}

/// Poll the token endpoint once. Front-end paces polls by `interval` from
/// `sign_in_start` (bumped on `SlowDown`).
#[tauri::command]
pub fn sign_in_poll(state: State<'_, SignInState>) -> SignInPollResult {
    let device_code = match state.inner.lock().expect("SignInState poisoned").as_ref() {
        Some(s) => s.device_code.clone(),
        None => {
            return SignInPollResult::Error {
                message: "sign_in_start was not called".to_string(),
            }
        }
    };

    let payload = serde_json::json!({
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    });

    match http_agent()
        .post(&format!("{CONTROL_API_URL}/auth/device/token"))
        .set("Accept", "application/json")
        .send_json(payload)
    {
        Ok(resp) => {
            let body: TokenResponse = match resp.into_json() {
                Ok(v) => v,
                Err(e) => {
                    return SignInPollResult::Error {
                        message: format!("token response malformed: {e}"),
                    }
                }
            };
            // Reject empty/missing access_token: storing Some("") would let
            // require_token() hand out an empty bearer downstream.
            if body.access_token.is_empty() {
                return SignInPollResult::Error {
                    message: "token response missing access_token".to_string(),
                };
            }
            let access_token = body.access_token;
            let refresh_token = body.refresh_token;
            let user_id = body.user.id;
            let email = body.user.email;
            let username = body.user.username;

            if let Some(session) = state.inner.lock().expect("SignInState poisoned").as_mut() {
                session.access_token = Some(access_token);
                session.refresh_token = refresh_token;
                session.user_id = Some(user_id.clone());
                session.email = Some(email.clone());
            }

            SignInPollResult::Approved {
                user_id,
                email,
                username,
            }
        }
        // RFC §3.5 non-success codes come as HTTP 400 with `detail.error` set.
        Err(ureq::Error::Status(400, resp)) => {
            let body: serde_json::Value = resp.into_json().unwrap_or_default();
            let err = body
                .get("detail")
                .and_then(|d| d.get("error"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            match err {
                "authorization_pending" => SignInPollResult::Pending,
                "slow_down" => SignInPollResult::SlowDown,
                "access_denied" => SignInPollResult::Denied,
                "expired_token" => SignInPollResult::Expired,
                other => SignInPollResult::Error {
                    message: format!("unexpected device-flow error: {other}"),
                },
            }
        }
        // Control-plane 429 — fold into slow_down so the front-end backs off.
        Err(ureq::Error::Status(429, _)) => SignInPollResult::SlowDown,
        Err(e) => SignInPollResult::Error {
            message: format!("token poll failed: {e}"),
        },
    }
}

// --- Register device against Control -----------------------------------------

/// Mirrors backend's `RegisterWithKeyResponse`. `config` is the AgentConfig
/// the runtime expects on disk; passed to `install_agent_config` verbatim.
#[derive(Serialize)]
pub struct RegisteredDevice {
    pub device_id: String,
    pub api_key: String,
    pub config: serde_json::Value,
}

/// Mint a single-use registration key. Split from `register_device` so the UI
/// can render distinct progress states — register-with-key can take 5-10s cold.
#[tauri::command]
pub fn mint_registration_key(state: State<'_, SignInState>) -> Result<String, String> {
    let token = require_token(&state)?;

    let mint_body = serde_json::json!({
        "ttl_hours": 1,
        "registration_source": "onboarding_wizard",
    });
    let key_resp = http_agent()
        .post(&format!("{CONTROL_API_URL}/devices/registration-keys"))
        .set("Accept", "application/json")
        .set("Authorization", &format!("Bearer {token}"))
        .send_json(mint_body)
        .map_err(|e| describe_ureq_err("registration-keys mint", e))?;
    let key_body: serde_json::Value = key_resp
        .into_json()
        .map_err(|e| format!("registration-keys response malformed: {e}"))?;
    let registration_key = key_body
        .get("registration_key")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "registration_key missing from response".to_string())?
        .to_string();
    Ok(registration_key)
}

/// Redeem a registration key for a (device_id, api_key, AgentConfig) triple.
/// Requires the caller to be signed in — JWT is used in addition to the key.
#[tauri::command]
pub fn register_device(
    state: State<'_, SignInState>,
    device_name: String,
    registration_key: String,
) -> Result<RegisteredDevice, String> {
    let token = require_token(&state)?;

    // Include the installed agent version so Control doesn't show "unknown"
    // until the first lifecycle heartbeat lands post-restart.
    let mut metadata = serde_json::json!({
        "os": std::env::consts::OS,
        "arch": std::env::consts::ARCH,
        "source": "setup_assistant",
    });
    if let Some(v) = installed_version(&PathBuf::from(resolve_install_root())) {
        metadata["agent_version"] = serde_json::Value::String(v.version);
    }
    let register_body = serde_json::json!({
        "registration_key": registration_key,
        "name": device_name,
        "device_type": "other",
        "metadata": metadata,
    });
    let reg_resp = http_agent()
        .post(&format!("{CONTROL_API_URL}/devices/register-with-key"))
        .set("Accept", "application/json")
        .set("Authorization", &format!("Bearer {token}"))
        .send_json(register_body)
        .map_err(|e| describe_ureq_err("register-with-key", e))?;
    let reg_body: serde_json::Value = reg_resp
        .into_json()
        .map_err(|e| format!("register-with-key response malformed: {e}"))?;

    let device_id = reg_body
        .get("device_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "device_id missing from response".to_string())?
        .to_string();
    let api_key = reg_body
        .get("api_key")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "api_key missing from response".to_string())?
        .to_string();
    let config = reg_body
        .get("config")
        .cloned()
        .unwrap_or(serde_json::Value::Null);

    Ok(RegisteredDevice {
        device_id,
        api_key,
        config,
    })
}

/// Write `config` to `<install_root>/configs/session_<UTC>.json` — the location
/// the runtime's `StateManager` picks up on next start. Returns the written path.
#[tauri::command]
pub fn install_agent_config(
    install_root: String,
    config: serde_json::Value,
) -> Result<String, String> {
    if config.is_null() {
        return Err("config from register_device was null — nothing to write".to_string());
    }

    // Backend ships topics with unfilled `${identity.*}` placeholders; writing
    // them verbatim would make the runtime pub/sub to literal "${...}" paths.
    // Substitute from the identity block; unknown placeholders pass through
    // (matches Python's resolve_templates() so markers like {cid}/{mid} survive).
    let identity = config.get("identity").cloned().unwrap_or_default();
    let context = serde_json::json!({ "identity": identity });
    let config = resolve_config_templates(&config, &context);

    let root = PathBuf::from(&install_root);
    // macOS root is laid down by the .pkg postinstall; a missing root means
    // the install itself is broken. Linux has no pkg step, so the create_dir_all
    // below will lay it down on demand.
    #[cfg(target_os = "macos")]
    {
        if !root.exists() {
            return Err(format!(
                "install root not found at {}. Is Locai Link installed?",
                root.display()
            ));
        }
    }
    // configs/ lives outside current/ so session state survives OTA version flips.
    let configs_dir = root.join("configs");
    std::fs::create_dir_all(&configs_dir)
        .map_err(|e| format!("create configs dir {}: {e}", configs_dir.display()))?;

    // Filename mirrors StateManager.bootstrap()'s format: session_<UTC>.json.
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| format!("system clock: {e}"))?;
    let secs = now.as_secs();
    let ts = format_utc_compact(secs);
    let session_path = configs_dir.join(format!("session_{ts}.json"));

    let serialized =
        serde_json::to_string_pretty(&config).map_err(|e| format!("serialise config: {e}"))?;
    std::fs::write(&session_path, serialized)
        .map_err(|e| format!("write {}: {e}", session_path.display()))?;

    // 0600 — the session file contains the device api_key.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perms = std::fs::metadata(&session_path)
            .map_err(|e| format!("stat {}: {e}", session_path.display()))?
            .permissions();
        perms.set_mode(0o600);
        std::fs::set_permissions(&session_path, perms)
            .map_err(|e| format!("chmod {}: {e}", session_path.display()))?;
    }

    Ok(session_path.to_string_lossy().into_owned())
}

// --- Model catalog (list + deploy) ------------------------------------------

/// Subset of the backend's `ModelResponse` — only the fields the wizard renders.
#[derive(Serialize)]
pub struct ModelSummary {
    pub id: String,
    pub display_name: String,
    pub model_type: String,
    pub framework: String,
    pub file_extension: String,
    pub size_bytes: u64,
    pub status: String,
}

/// Turn a ureq error into a display string; preserves the server's `detail`
/// body on non-2xx, which ureq's default Display drops.
fn describe_ureq_err(op: &str, err: ureq::Error) -> String {
    match err {
        ureq::Error::Status(code, resp) => {
            let url = resp.get_url().to_string();
            match resp.into_string() {
                Ok(body) if !body.is_empty() => {
                    format!("{op} failed: HTTP {code} from {url} — {body}")
                }
                _ => format!("{op} failed: HTTP {code} from {url}"),
            }
        }
        ureq::Error::Transport(t) => format!("{op} failed (transport): {t}"),
    }
}

/// Read the JWT out of `SignInState`, or return the "sign in first" error.
fn require_token(state: &State<'_, SignInState>) -> Result<String, String> {
    state
        .inner
        .lock()
        .expect("SignInState poisoned")
        .as_ref()
        .and_then(|s| s.access_token.clone())
        .ok_or_else(|| "not signed in — sign in first".to_string())
}

/// List models visible to the signed-in user. Hits `list_without_layers_info`
/// to skip the ~MB of per-layer detail.
#[tauri::command]
pub fn list_models(state: State<'_, SignInState>) -> Result<Vec<ModelSummary>, String> {
    let token = require_token(&state)?;

    let resp = http_agent()
        .get(&format!(
            "{CONTROL_API_URL}/models/list_without_layers_info"
        ))
        .set("Accept", "application/json")
        .set("Authorization", &format!("Bearer {token}"))
        .call()
        .map_err(|e| describe_ureq_err("list_models", e))?;

    let body: serde_json::Value = resp
        .into_json()
        .map_err(|e| format!("list_models response malformed: {e}"))?;

    let arr = body
        .as_array()
        .ok_or_else(|| "list_models: expected JSON array".to_string())?;

    // Drop entries missing id/display_name — the UI can't render them.
    let models = arr
        .iter()
        .filter_map(|m| {
            let id = m.get("id")?.as_str()?.to_string();
            let display_name = m.get("display_name")?.as_str()?.to_string();
            let model_type = m
                .get("model_type")
                .and_then(|v| v.as_str())
                .unwrap_or("other")
                .to_string();
            let framework = m
                .get("framework")
                .and_then(|v| v.as_str())
                .unwrap_or("Other")
                .to_string();
            let file_extension = m
                .get("file_extension")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let size_bytes = m.get("size_bytes").and_then(|v| v.as_u64()).unwrap_or(0);
            let status = m
                .get("status")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown")
                .to_string();
            Some(ModelSummary {
                id,
                display_name,
                model_type,
                framework,
                file_extension,
                size_bytes,
                status,
            })
        })
        .collect();

    Ok(models)
}

/// Pre-register a model as "queued" so the Models panel shows it at 0%
/// immediately — the runtime processes deploys serially, so without this the
/// panel only reveals models one at a time.
#[tauri::command]
pub fn mark_deployment_pending(
    pipeline_id: String,
    model_name: Option<String>,
) -> Result<(), String> {
    #[derive(serde::Serialize)]
    struct Body<'a> {
        pipeline_id: &'a str,
        model_name: Option<&'a str>,
    }
    let body = Body {
        pipeline_id: &pipeline_id,
        model_name: model_name.as_deref(),
    };
    let url = crate::shared::DEFAULT_PENDING_URL;
    let payload = serde_json::to_value(&body).unwrap();

    // `install_launchagents` returns as soon as fork() succeeds, but the
    // health server binds a beat later — retry with backoff during that window.
    let mut attempts_left = 10u32;
    let mut delay_ms = 200u64;
    loop {
        match http_agent().post(url).send_json(payload.clone()) {
            Ok(_) => return Ok(()),
            Err(e) => {
                attempts_left = attempts_left.saturating_sub(1);
                if attempts_left == 0 {
                    return Err(format!("mark_deployment_pending: {e}"));
                }
                std::thread::sleep(std::time::Duration::from_millis(delay_ms));
                // Linear — the runtime either comes up quickly or is broken.
                delay_ms = (delay_ms + 100).min(600);
            }
        }
    }
}

/// Block until the agent is reachable AND its Zenoh transport reports
/// `connected: true`. Otherwise Control's DEPLOY_MODEL races the agent's
/// subscriber setup and the command is dropped (deploy accepted, no download).
/// Polls up to ~15 s to cover PyInstaller unpack + zenoh handshake on slow disks.
#[tauri::command]
pub async fn wait_for_agent_ready() -> Result<(), String> {
    // 15 s of polling on the main thread froze the setup window; hop onto the
    // blocking pool so the WebView stays responsive during the wait.
    tauri::async_runtime::spawn_blocking(|| {
        let url = crate::shared::DEFAULT_HEALTH_URL;
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
        #[allow(unused_assignments)]
        let mut last_err = String::from("agent never came up");
        loop {
            match http_agent().get(url).call() {
                Ok(resp) => match resp.into_json::<serde_json::Value>() {
                    Ok(body) => {
                        let connected = body
                            .get("transport")
                            .and_then(|t| t.get("connected"))
                            .and_then(|c| c.as_bool())
                            .unwrap_or(false);
                        if connected {
                            // Settle delay so Zenoh queryable/subscriber
                            // registrations propagate to Control before the
                            // next `deploy_model` call races them.
                            std::thread::sleep(std::time::Duration::from_millis(400));
                            return Ok(());
                        }
                        last_err = "transport not connected yet".to_string();
                    }
                    Err(e) => {
                        last_err = format!("malformed /healthz: {e}");
                    }
                },
                Err(e) => {
                    last_err = format!("{e}");
                }
            }
            if std::time::Instant::now() >= deadline {
                return Err(format!("wait_for_agent_ready: {last_err}"));
            }
            std::thread::sleep(std::time::Duration::from_millis(300));
        }
    })
    .await
    .expect("wait_for_agent_ready panicked")
}

/// Queue a deploy of `model_id` onto `device_id`; returns the deployment id.
/// Enqueue-only — Control dispatches via Zenoh, runtime downloads later.
#[tauri::command]
pub fn deploy_model(
    state: State<'_, SignInState>,
    device_id: String,
    model_id: String,
) -> Result<String, String> {
    let token = require_token(&state)?;

    let resp = http_agent_with_timeout(DEPLOY_TIMEOUT)
        .post(&format!(
            "{CONTROL_API_URL}/models/{model_id}/deploy/{device_id}"
        ))
        .set("Accept", "application/json")
        .set("Authorization", &format!("Bearer {token}"))
        // `.send_bytes(&[])` sets Content-Length: 0 — `.call()` on a POST
        // omits it and Google's L7 LB rejects with HTTP 411 Length Required.
        .send_bytes(&[])
        .map_err(|e| describe_ureq_err("deploy_model", e))?;

    let body: serde_json::Value = resp
        .into_json()
        .map_err(|e| format!("deploy_model response malformed: {e}"))?;

    body.get("id")
        .and_then(|v| v.as_str())
        .map(String::from)
        .ok_or_else(|| "deployment id missing from response".to_string())
}

/// Recursively substitute `${path.to.key}` placeholders. Mirrors the runtime's
/// `resolve_templates` — unknown placeholders pass through so per-emit markers survive.
fn resolve_config_templates(
    value: &serde_json::Value,
    context: &serde_json::Value,
) -> serde_json::Value {
    match value {
        serde_json::Value::String(s) => {
            serde_json::Value::String(resolve_template_string(s, context))
        }
        serde_json::Value::Array(arr) => serde_json::Value::Array(
            arr.iter()
                .map(|v| resolve_config_templates(v, context))
                .collect(),
        ),
        serde_json::Value::Object(map) => {
            let mut out = serde_json::Map::with_capacity(map.len());
            for (k, v) in map {
                out.insert(k.clone(), resolve_config_templates(v, context));
            }
            serde_json::Value::Object(out)
        }
        _ => value.clone(),
    }
}

fn resolve_template_string(s: &str, context: &serde_json::Value) -> String {
    let mut result = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(open) = rest.find("${") {
        result.push_str(&rest[..open]);
        let after_open = &rest[open + 2..];
        match after_open.find('}') {
            Some(close) => {
                let path = &after_open[..close];
                match lookup_context_path(context, path) {
                    Some(v) => result.push_str(&v),
                    // Preserve unknown placeholders for the runtime to substitute later.
                    None => result.push_str(&rest[open..open + 2 + close + 1]),
                }
                rest = &after_open[close + 1..];
            }
            None => {
                // Unclosed "${" — copy rest verbatim.
                result.push_str(&rest[open..]);
                return result;
            }
        }
    }
    result.push_str(rest);
    result
}

fn lookup_context_path(context: &serde_json::Value, path: &str) -> Option<String> {
    let mut node = context;
    for part in path.split('.') {
        node = node.get(part)?;
    }
    match node {
        // Preserve the placeholder rather than emit "null".
        serde_json::Value::Null => None,
        // .to_string() on a JSON string wraps it in escaped quotes — .clone().
        serde_json::Value::String(s) => Some(s.clone()),
        other => Some(other.to_string()),
    }
}

// --- LaunchAgent plist install (macOS) ---------------------------------------

/// Install the LaunchAgent plist into `~/Library/LaunchAgents/` and set its
/// `RunAtLoad` from the toggle. It does NOT bootstrap/kickstart: the pkg
/// postinstall already bootstrapped the agent so launchd owns the one running
/// instance, and kickstarting here would restart the wizard mid-onboarding.
/// `RunAtLoad` takes effect at next login; `finish_setup` re-arms the supervisor.
#[tauri::command]
#[cfg(target_os = "macos")]
pub fn install_launchagents(install_root: String, run_at_login: bool) -> Result<(), String> {
    let root = PathBuf::from(&install_root);
    let source_dir = root.join("LaunchAgents");
    if !source_dir.is_dir() {
        return Err(format!(
            "LaunchAgents source not found at {}",
            source_dir.display()
        ));
    }

    let home = std::env::var("HOME").map_err(|_| "$HOME not set".to_string())?;
    let dest_dir = PathBuf::from(home).join("Library").join("LaunchAgents");
    std::fs::create_dir_all(&dest_dir)
        .map_err(|e| format!("create {}: {e}", dest_dir.display()))?;

    // A signed .pkg shouldn't attach com.apple.quarantine, but some macOS
    // versions / MDM paths do — and launchctl-driven launches sit silently
    // blocked when it's present. `xattr -dr` is a no-op when the attr is absent.
    for path in [
        "/Applications/Locai Link.app",
        "/Library/Locai/Locai Link.app",
    ] {
        let _ = std::process::Command::new("xattr")
            .args(["-dr", "com.apple.quarantine", path])
            .output();
    }

    let uid = crate::preferences::current_uid()?;

    // LEGACY-SA-CLEANUP: pre-merge shipped a separate agent LaunchAgent that ran
    // the runtime; the merged binary supervises it in-process now. Boot out +
    // remove the old agent unit on upgrade.
    let _ = std::process::Command::new("launchctl")
        .args(["bootout", &format!("gui/{uid}/{AGENT_LABEL}")])
        .output();
    let _ = std::fs::remove_file(dest_dir.join("uk.co.locai.link.agent.plist"));

    // The single unit: the app plist runs the merged `locai-link` binary,
    // which supervises the runtime child + shows the tray.
    let plist_name = "uk.co.locai.link.companion.plist";
    let src = source_dir.join(plist_name);
    let dst = dest_dir.join(plist_name);
    std::fs::copy(&src, &dst)
        .map_err(|e| format!("copy {} -> {}: {e}", src.display(), dst.display()))?;

    // macOS 12+ rejects plists that aren't 0644 with "Bootstrap failed: 5".
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perms = std::fs::metadata(&dst)
            .map_err(|e| format!("stat {}: {e}", dst.display()))?
            .permissions();
        perms.set_mode(0o644);
        std::fs::set_permissions(&dst, perms)
            .map_err(|e| format!("chmod 644 {}: {e}", dst.display()))?;
    }

    // Source plist ships with RunAtLoad=true; only touch when opted out.
    if !run_at_login {
        let status = std::process::Command::new("/usr/libexec/PlistBuddy")
            .args(["-c", "Set :RunAtLoad false", dst.to_str().unwrap_or("")])
            .status()
            .map_err(|e| format!("PlistBuddy: {e}"))?;
        if !status.success() {
            return Err(format!(
                "PlistBuddy failed to set RunAtLoad=false on {}",
                dst.display()
            ));
        }
    }

    // The launch + launchd ownership is the pkg postinstall's job now: it
    // bootstraps the LaunchAgent so launchd owns the one instance from install
    // (parity with the Linux service, now that the tauri single-instance guard is
    // gone). Finish must NOT bootstrap/kickstart its own running process —
    // `kickstart -k` would restart the wizard mid-onboarding (suicide). Setting
    // RunAtLoad above is enough; it takes effect at next login. `finish_setup`
    // re-arms the supervisor via `control.start()`.
    Ok(())
}

/// Linux equivalent of the macOS LaunchAgent bootstrap. Copies staged units
/// into `~/.config/systemd/user/`, reloads, then enables-and-starts or
/// just starts based on the toggle. Idempotent.
#[tauri::command]
#[cfg(target_os = "linux")]
pub fn install_launchagents(install_root: String, run_at_login: bool) -> Result<(), String> {
    let root = PathBuf::from(&install_root);
    let source_dir = root.join("systemd");
    if !source_dir.is_dir() {
        return Err(format!(
            "systemd units source not found at {}",
            source_dir.display()
        ));
    }

    let home = std::env::var("HOME").map_err(|_| "$HOME not set".to_string())?;
    let dest_dir = PathBuf::from(&home)
        .join(".config")
        .join("systemd")
        .join("user");
    std::fs::create_dir_all(&dest_dir)
        .map_err(|e| format!("create {}: {e}", dest_dir.display()))?;

    // LEGACY-SA-CLEANUP: pre-merge shipped a separate agent unit that ran the
    // runtime; the merged binary supervises it in-process now. Stop + remove it
    // on upgrade.
    let _ = std::process::Command::new("systemctl")
        .args(["--user", "disable", "--now", "locai-link-agent.service"])
        .output();
    let _ = std::fs::remove_file(dest_dir.join("locai-link-agent.service"));

    // The single unit runs the merged `locai-link` binary (supervises the
    // runtime child + shows the tray).
    let unit = "locai-link-companion.service";
    let src = source_dir.join(unit);
    let dst = dest_dir.join(unit);
    std::fs::copy(&src, &dst)
        .map_err(|e| format!("copy {} -> {}: {e}", src.display(), dst.display()))?;
    {
        // systemd rejects non-0644 units; fs::copy carries source mode through.
        use std::os::unix::fs::PermissionsExt;
        let mut perms = std::fs::metadata(&dst)
            .map_err(|e| format!("stat {}: {e}", dst.display()))?
            .permissions();
        perms.set_mode(0o644);
        std::fs::set_permissions(&dst, perms)
            .map_err(|e| format!("chmod 644 {}: {e}", dst.display()))?;
    }

    let reload = std::process::Command::new("systemctl")
        .args(["--user", "daemon-reload"])
        .output()
        .map_err(|e| format!("systemctl --user daemon-reload: {e}"))?;
    if !reload.status.success() {
        return Err(format!(
            "systemctl --user daemon-reload failed: {}",
            String::from_utf8_lossy(&reload.stderr).trim()
        ));
    }

    // disable first so unchecking the toggle on re-run has a clean transition.
    let _ = std::process::Command::new("systemctl")
        .args(["--user", "disable", unit])
        .output();

    let args: &[&str] = if run_at_login {
        &["--user", "enable", "--now", unit]
    } else {
        &["--user", "start", unit]
    };
    let out = std::process::Command::new("systemctl")
        .args(args)
        .output()
        .map_err(|e| format!("systemctl {args:?}: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "systemctl {args:?} exited {:?}: {} {}",
            out.status.code(),
            String::from_utf8_lossy(&out.stdout).trim(),
            String::from_utf8_lossy(&out.stderr).trim(),
        ));
    }

    Ok(())
}

#[tauri::command]
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub fn install_launchagents(_install_root: String, _run_at_login: bool) -> Result<(), String> {
    Ok(())
}

/// Wipe local state for re-register: best-effort Control delete, stop runtime,
/// remove session file + downloaded models + pipeline state. Install itself
/// (binaries, units, launcher, versions) stays intact.
#[tauri::command]
pub fn re_register(
    state: State<'_, SignInState>,
    control: State<'_, crate::supervisor::SupervisorControl>,
    install_root: String,
    old_device_id: String,
) -> Result<(), String> {
    // 1. Control-side delete — best-effort; failure doesn't block the local wipe.
    if !old_device_id.is_empty() {
        if let Ok(token) = require_token(&state) {
            // Percent-encode even though device_id is expected to be a UUID: it
            // arrives from the frontend, so a stray & or # mustn't rewrite the query.
            let url = format!(
                "{CONTROL_API_URL}/devices/delete_device_by_id?device_id={}",
                crate::shared::encode_segment(&old_device_id)
            );
            match http_agent()
                .delete(&url)
                .set("Authorization", &format!("Bearer {token}"))
                .call()
            {
                Ok(_) => {}
                Err(e) => eprintln!("[re_register] Control DELETE failed (continuing): {e}"),
            }
        }
    }

    // 2. Stop the runtime child (via the in-process supervisor) so it releases
    //    its state files before the wipe. We do NOT stop the app's own unit —
    //    that would kill this window mid-re-register; the tray/supervisor stay
    //    up and the re-run wizard restarts the runtime on Finish.
    control.stop();
    // Give the supervise loop a beat to actually kill the child before we wipe.
    std::thread::sleep(std::time::Duration::from_millis(500));

    // 3. Nuke session files + downloaded models + pipeline state. Keep the
    //    rest so the fresh wizard doesn't re-install what's already on disk.
    let root = PathBuf::from(&install_root);
    for name in ["configs", "models", "state"] {
        let dir = root.join(name);
        if !dir.exists() {
            continue;
        }
        if let Err(e) = std::fs::remove_dir_all(&dir) {
            return Err(format!("failed to remove {}: {e}", dir.display()));
        }
    }
    Ok(())
}

/// Finish onboarding: re-arm the supervisor, dismiss the setup window, leave the
/// app in the tray. The device is registered by this point, so `control.start()`
/// lets the supervise loop spawn the runtime (it idled through onboarding, and
/// `re_register` explicitly stops it). Does NOT open Preferences — the user
/// reaches it from the tray. macOS drops back to Accessory so no Dock icon lingers.
#[tauri::command]
pub fn finish_setup(
    app: tauri::AppHandle,
    control: State<'_, crate::supervisor::SupervisorControl>,
) {
    control.start();
    if let Some(setup) = app.get_webview_window("setup") {
        let _ = setup.hide();
    }
    // No window on screen now; let the tray menu rebuild resume (it is deferred
    // while a window is visible to avoid miniaturizing it).
    crate::tray::WINDOW_VISIBLE.store(false, std::sync::atomic::Ordering::Relaxed);
    #[cfg(target_os = "macos")]
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
}

/// Reveal the Preferences window and dismiss the setup window. Used by the
/// "already set up" splash's explicit "Open Preferences" action.
#[tauri::command]
pub fn open_preferences_window(app: tauri::AppHandle) {
    crate::tray::show_preferences_window(&app);
    if let Some(setup) = app.get_webview_window("setup") {
        let _ = setup.hide();
    }
}

/// Best-effort device self-deregister before uninstall.
///
/// Reads the device identity from the session and asks Control to delete this
/// device, so its dashboard row is removed instead of lingering as offline.
/// Never fails the uninstall: a 404 (already gone) is treated as success; any
/// error (offline, or a rejected key → 401) is logged and swallowed. The
/// uninstaller's local wipe is the source of truth.
#[cfg(any(target_os = "macos", target_os = "linux"))]
fn best_effort_deregister() {
    match read_identity(&PathBuf::from(resolve_install_root())) {
        Some(id) => match deregister_device(&id) {
            Ok(()) => eprintln!("[setup] device deregistered from Control"),
            Err(e) => eprintln!("[setup] deregister failed (continuing uninstall): {e}"),
        },
        None => eprintln!("[setup] no device identity found; skipping deregister"),
    }
}

/// Fire the uninstaller (setup splash or Preferences danger zone).
/// `systemd-run --user --collect` on Linux so the script survives the runtime +
/// app being killed mid-run.
#[tauri::command]
#[cfg(target_os = "linux")]
pub fn launch_uninstaller(_install_root: String) -> Result<(), String> {
    // Ignore the frontend-supplied install_root and resolve canonically,
    // mirroring the macOS path: best_effort_deregister() runs below, so an
    // attacker-chosen script path must not become executable code here.
    let script = format!("{}/uninstall.sh", resolve_install_root());
    if !std::path::Path::new(&script).exists() {
        return Err(format!("uninstall.sh not found at {script}"));
    }
    // Deregister first: it needs the session api_key the uninstaller wipes. If
    // the spawn below fails, the device is deregistered but still installed
    // (recoverable on retry).
    best_effort_deregister();
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
#[cfg(target_os = "macos")]
pub fn launch_uninstaller(_install_root: String) -> Result<(), String> {
    // Ignore the frontend-supplied install_root and resolve canonically — the
    // uninstaller runs with admin privileges, so no injectable path on that surface.
    let script = format!("{}/uninstall.sh", resolve_install_root());
    if !std::path::Path::new(&script).exists() {
        return Err(format!("uninstall.sh not found at {script}"));
    }
    // Acquire admin auth up-front with a no-op command. If the user dismisses
    // the dialog, nothing has happened yet — no deregister, no removal — so a
    // cancel is a true no-op. We report a "cancelled" sentinel the UI treats as
    // benign (not a failure). macOS caches the granted credential for the
    // security session, so the uninstaller run below reuses it without a second
    // prompt.
    let auth = std::process::Command::new("osascript")
        .args([
            "-e",
            "do shell script \"true\" with administrator privileges",
        ])
        .output()
        .map_err(|e| format!("osascript: {e}"))?;
    if !auth.status.success() {
        return Err(uninstall_err(&auth.stderr, "admin authorization failed"));
    }
    // Auth granted — now it's safe to deregister (it needs the session api_key
    // the uninstaller wipes) and run the uninstaller. If the run below somehow
    // fails, the device is deregistered but still installed (recoverable on retry).
    best_effort_deregister();
    // AppleScript's `quoted form of` safely escapes the shell argument; `&`
    // concatenation keeps the path inside AppleScript's own string escaping.
    //
    // Run DETACHED (nohup + background, output to a temp log) so the script
    // survives the app being killed mid-run. The uninstaller kills the main app
    // in one of its early steps; run synchronously it would tear down its own
    // osascript subtree (it is a child of the app) and never reach the file
    // removal, leaving the install behind. This mirrors the Linux path, which
    // detaches via `systemd-run --collect` for the same reason.
    let escaped_path = script.replace('\\', "\\\\").replace('"', "\\\"");
    let apple_script = format!(
        "do shell script \"nohup /bin/bash \" & quoted form of \"{escaped_path}\" & \" >/tmp/locai-uninstall.log 2>&1 &\" with administrator privileges"
    );
    let out = std::process::Command::new("osascript")
        .args(["-e", &apple_script])
        .output()
        .map_err(|e| format!("osascript: {e}"))?;
    if !out.status.success() {
        return Err(uninstall_err(&out.stderr, "uninstall.sh failed"));
    }
    Ok(())
}

/// Map an osascript failure to a UI error string. A dismissed admin dialog
/// (AppleScript "User canceled", error -128) becomes the `"cancelled"` sentinel
/// the frontend treats as a benign no-op; anything else keeps `context` + stderr.
#[cfg(target_os = "macos")]
fn uninstall_err(stderr: &[u8], context: &str) -> String {
    let msg = String::from_utf8_lossy(stderr);
    if msg.contains("User canceled") || msg.contains("-128") {
        return "cancelled".to_string();
    }
    format!("{context}: {}", msg.trim())
}

#[tauri::command]
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub fn launch_uninstaller(_install_root: String) -> Result<(), String> {
    Err("launch_uninstaller: unsupported platform".to_string())
}

/// Quit the whole app. Used by the uninstall flow (the app is being removed, so
/// the tray must go too); `close()` on macOS only hides the window.
#[tauri::command]
pub fn exit_app(app: tauri::AppHandle) {
    app.exit(0);
}

/// UTC seconds → `YYYYMMDD_HHMMSS`. Enough date arithmetic to skip chrono.
fn format_utc_compact(unix_secs: u64) -> String {
    let days = unix_secs / 86_400;
    let tod = unix_secs % 86_400;
    let h = tod / 3600;
    let m = (tod % 3600) / 60;
    let s = tod % 60;

    // Civil-from-days (Howard Hinnant, http://howardhinnant.github.io/date_algorithms.html).
    let z = days as i64 + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if month <= 2 { y + 1 } else { y };

    format!(
        "{year:04}{month:02}{d:02}_{h:02}{m:02}{s:02}",
        year = year,
        month = month,
        d = d,
        h = h,
        m = m,
        s = s
    )
}
