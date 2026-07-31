// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::State;

use locai_link_shared::{
    deregister_device, installed_version, read_boot_json, read_identity,
    supported_model_types as shared_supported_model_types, BootConfig,
};

// Overridable at build time via `LOCAI_CONTROL_API_URL` (dev builds); unset
// defaults to prod. Registration writes this into the session config, so every
// device-authenticated call (deregister, uninstall-report) follows the same env.
const CONTROL_API_URL: &str = match option_env!("LOCAI_CONTROL_API_URL") {
    Some(url) => url,
    None => "https://api.locai.co.uk/api/v1",
};

/// LaunchAgent labels — must match `bundling/pkg/LaunchAgents/*.plist`.
#[cfg(target_os = "macos")]
const AGENT_LABEL: &str = "uk.co.locai.link.agent";
#[cfg(target_os = "macos")]
const COMPANION_LABEL: &str = "uk.co.locai.link.companion";

/// Generous because the backend hits Firestore synchronously on both paths.
const HTTP_TIMEOUT: Duration = Duration::from_secs(15);

/// Longer per-attempt budget for the device-code request: the first post-boot
/// call can wake a scaled-to-zero backend, and a cold start exceeding
/// HTTP_TIMEOUT surfaced as a sign-in timeout that only cleared on retry.
const DEVICE_CODE_TIMEOUT: Duration = Duration::from_secs(45);

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
fn suggest_device_name() -> String {
    machine_hostname()
}

// --- Check Install -----------------------------------------------------------

/// Wire-format result for the SA's "Check Install" step.
#[derive(Serialize)]
pub struct CheckInstallResult {
    pub installed: bool,
    pub version: Option<String>,
    pub path: Option<String>,
    pub boot: Option<BootConfig>,
    /// Distinct from `reason` so the UI can say "install found but config is broken".
    pub boot_error: Option<String>,
    pub reason: Option<String>,
    /// From the newest `session_*.json`; when set, the SA renders the
    /// "already set up" splash instead of the wizard.
    pub device_id: Option<String>,
    pub device_name: Option<String>,
}

fn resolve_install_root() -> String {
    locai_link_shared::install_root()
}

/// Install root, keyed on host OS. Mirrored in the companion's `install_root`.
#[tauri::command]
fn get_install_root() -> String {
    resolve_install_root()
}

/// OS the SA is running on, so the frontend can render platform-appropriate strings.
#[tauri::command]
fn get_platform() -> String {
    std::env::consts::OS.to_string()
}

/// Model types this build can serve, derived from the installed bundle's manifest
/// plugins. The installer list filters to these so an LLM-only build
/// never offers audio/other models it can't run.
#[tauri::command]
fn supported_model_types() -> Vec<String> {
    shared_supported_model_types(&PathBuf::from(resolve_install_root()))
}

/// Read the on-disk install state. Never `Err` — the failure modes are legitimate
/// outcomes the UI needs to render; `reason` / `boot_error` carry the detail.
#[tauri::command]
fn check_install(install_root: String) -> CheckInstallResult {
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

/// Pull `(device_id, device_name)` from the newest `session_*.json`, best-effort.
fn read_registered_identity(install_root: &Path) -> (Option<String>, Option<String>) {
    let configs = install_root.join("configs");
    let entries = match std::fs::read_dir(&configs) {
        Ok(e) => e,
        Err(_) => return (None, None),
    };
    let mut newest: Option<(std::time::SystemTime, PathBuf)> = None;
    for entry in entries.flatten() {
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
    let path = match newest {
        Some((_, p)) => p,
        None => return (None, None),
    };
    let body = match std::fs::read_to_string(&path) {
        Ok(s) => s,
        Err(_) => return (None, None),
    };
    let json: serde_json::Value = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(_) => return (None, None),
    };
    let identity = match json.get("identity") {
        Some(v) => v,
        None => return (None, None),
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
async fn sign_in_start(state: State<'_, SignInState>) -> Result<DeviceCodeStart, String> {
    // HTTP + optional retry sleep runs on the blocking pool so the SA
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
fn sign_in_poll(state: State<'_, SignInState>) -> SignInPollResult {
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
fn mint_registration_key(state: State<'_, SignInState>) -> Result<String, String> {
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
fn register_device(
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
fn install_agent_config(install_root: String, config: serde_json::Value) -> Result<String, String> {
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
fn list_models(state: State<'_, SignInState>) -> Result<Vec<ModelSummary>, String> {
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
fn mark_deployment_pending(pipeline_id: String, model_name: Option<String>) -> Result<(), String> {
    #[derive(serde::Serialize)]
    struct Body<'a> {
        pipeline_id: &'a str,
        model_name: Option<&'a str>,
    }
    let body = Body {
        pipeline_id: &pipeline_id,
        model_name: model_name.as_deref(),
    };
    let url = locai_link_shared::DEFAULT_PENDING_URL;
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
async fn wait_for_agent_ready() -> Result<(), String> {
    // 15 s of polling on the main thread froze the SA window; hop onto the
    // blocking pool so the WebView stays responsive during the wait.
    tauri::async_runtime::spawn_blocking(|| {
        let url = locai_link_shared::DEFAULT_HEALTH_URL;
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
fn deploy_model(
    state: State<'_, SignInState>,
    device_id: String,
    model_id: String,
) -> Result<String, String> {
    let token = require_token(&state)?;

    let resp = http_agent()
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

// --- LaunchAgent bootstrap (macOS) -------------------------------------------

/// Copy staged LaunchAgent plists into `~/Library/LaunchAgents/`, then bootstrap
/// + kickstart both. `run_at_login` only affects the plist's RunAtLoad — both
/// agents are always kickstarted now so the user sees the setup pay off immediately.
#[tauri::command]
#[cfg(target_os = "macos")]
fn install_launchagents(install_root: String, run_at_login: bool) -> Result<(), String> {
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

    // Must match bundling/pkg/LaunchAgents/.
    let agents: [(&str, &str); 2] = [
        ("uk.co.locai.link.agent.plist", "uk.co.locai.link.agent"),
        (
            "uk.co.locai.link.companion.plist",
            "uk.co.locai.link.companion",
        ),
    ];

    // A signed .pkg shouldn't attach com.apple.quarantine, but some macOS
    // versions / MDM paths do — and launchctl-driven launches sit silently
    // blocked when it's present. `xattr -dr` is a no-op when the attr is absent.
    let companion_app_paths = [
        "/Applications/Locai Link.app",
        "/Library/Locai/Locai Link.app",
    ];
    for path in companion_app_paths {
        let _ = std::process::Command::new("xattr")
            .args(["-dr", "com.apple.quarantine", path])
            .output();
    }

    // Collect per-service kickstart failures so the caller sees a real Err
    // rather than a silent-success + missing tray icon.
    let mut kickstart_failures: Vec<String> = Vec::new();

    for (plist_name, label) in agents {
        let src = source_dir.join(plist_name);
        let dst = dest_dir.join(plist_name);
        std::fs::copy(&src, &dst)
            .map_err(|e| format!("copy {} -> {}: {e}", src.display(), dst.display()))?;

        // macOS 12+ rejects plists that aren't 0644 with "Bootstrap failed: 5".
        // fs::copy carries source mode through — force canonical.
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&dst)
                .map_err(|e| format!("stat {}: {e}", dst.display()))?
                .permissions();
            perms.set_mode(0o644);
            std::fs::set_permissions(&dst, perms)
                .map_err(|e| format!("chmod 644 {}: {e}", dst.display()))?;
        }

        // Source plists ship with RunAtLoad=true; only touch when opted out.
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

        let uid = current_uid()?;
        let domain = format!("gui/{uid}");
        let service = format!("{domain}/{label}");

        // bootout-then-bootstrap so a re-install actually picks up the fresh
        // plist — bootstrap alone fails with "service already loaded" and
        // leaves the old ProgramArguments / RunAtLoad in effect.
        let _ = std::process::Command::new("launchctl")
            .args(["bootout", &service])
            .output();

        let bootstrap_out = std::process::Command::new("launchctl")
            .args(["bootstrap", &domain, dst.to_str().unwrap_or("")])
            .output()
            .map_err(|e| format!("launchctl bootstrap: {e}"))?;
        if !bootstrap_out.status.success() {
            // Log but keep going — kickstart may still succeed on a raced bootstrap.
            eprintln!(
                "[install_launchagents] bootstrap {service} failed ({:?}): {} {}",
                bootstrap_out.status.code(),
                String::from_utf8_lossy(&bootstrap_out.stdout).trim(),
                String::from_utf8_lossy(&bootstrap_out.stderr).trim(),
            );
        }

        // -k restarts if running, starts fresh otherwise. Kickstart is the
        // load-bearing step — if this fails the service is definitely not
        // running, so record it and surface at the end.
        let kickstart_out = std::process::Command::new("launchctl")
            .args(["kickstart", "-k", &service])
            .output()
            .map_err(|e| format!("launchctl kickstart: {e}"))?;
        if !kickstart_out.status.success() {
            let msg = format!(
                "kickstart {service} exited {:?}: {} {}",
                kickstart_out.status.code(),
                String::from_utf8_lossy(&kickstart_out.stdout).trim(),
                String::from_utf8_lossy(&kickstart_out.stderr).trim(),
            );
            eprintln!("[install_launchagents] {msg}");
            kickstart_failures.push(msg);
        }
    }

    // Fallback ONLY when kickstart didn't bring the service up. An unconditional
    // `open -a` starts a SECOND instance: it opens the /Applications copy while
    // launchd already runs the /Library/Locai copy (same bundle id, different
    // path), so LaunchServices spawns another tray. Gate it on kickstart failure.
    if !kickstart_failures.is_empty() {
        for path in [
            // Prefer the OTA-updated install-root copy over the pkg-managed
            // /Applications copy, matching updater.py::_restart_companion_macos.
            "/Library/Locai/Locai Link.app",
            "/Applications/Locai Link.app",
        ] {
            if std::path::Path::new(path).exists() {
                let _ = std::process::Command::new("open")
                    .args(["-a", path])
                    .output();
                break;
            }
        }
        return Err(kickstart_failures.join("\n"));
    }
    Ok(())
}

/// Linux equivalent of the macOS LaunchAgent bootstrap. Copies staged units
/// into `~/.config/systemd/user/`, reloads, then enables-and-starts or
/// just starts based on the toggle. Idempotent.
#[tauri::command]
#[cfg(target_os = "linux")]
fn install_launchagents(install_root: String, run_at_login: bool) -> Result<(), String> {
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

    let units: [&str; 2] = ["locai-link-agent.service", "locai-link-companion.service"];

    for unit in units {
        let src = source_dir.join(unit);
        let dst = dest_dir.join(unit);
        std::fs::copy(&src, &dst)
            .map_err(|e| format!("copy {} -> {}: {e}", src.display(), dst.display()))?;
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

    let mut start_failures: Vec<String> = Vec::new();

    for unit in units {
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
            let msg = format!(
                "systemctl {args:?} exited {:?}: {} {}",
                out.status.code(),
                String::from_utf8_lossy(&out.stdout).trim(),
                String::from_utf8_lossy(&out.stderr).trim(),
            );
            eprintln!("[install_launchagents] {msg}");
            start_failures.push(msg);
        }
    }

    if !start_failures.is_empty() {
        return Err(start_failures.join("\n"));
    }

    Ok(())
}

#[tauri::command]
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn install_launchagents(_install_root: String, _run_at_login: bool) -> Result<(), String> {
    Ok(())
}

/// UID via `id -u` — avoids pulling libc into an otherwise-libc-free crate.
#[cfg(target_os = "macos")]
fn current_uid() -> Result<String, String> {
    let out = std::process::Command::new("id")
        .arg("-u")
        .output()
        .map_err(|e| format!("id -u: {e}"))?;
    if !out.status.success() {
        return Err("id -u failed".to_string());
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Wipe local state for re-register: best-effort Control delete, stop runtime,
/// remove session file + downloaded models + pipeline state. Install itself
/// (binaries, units, launcher, versions) stays intact.
#[tauri::command]
fn re_register(
    state: State<'_, SignInState>,
    install_root: String,
    old_device_id: String,
) -> Result<(), String> {
    // 1. Control-side delete — best-effort; failure doesn't block the local wipe.
    if !old_device_id.is_empty() {
        if let Ok(token) = require_token(&state) {
            // device_id is a UUID — no percent-encoding needed. Add urlencoding
            // if Control ever accepts non-UUID ids.
            let url =
                format!("{CONTROL_API_URL}/devices/delete_device_by_id?device_id={old_device_id}");
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

    // 2. Stop runtime + companion so they release state files before delete.
    //    `stop` preserves the enable state, so the toggle survives.
    #[cfg(target_os = "linux")]
    {
        for unit in ["locai-link-agent.service", "locai-link-companion.service"] {
            let _ = std::process::Command::new("systemctl")
                .args(["--user", "stop", unit])
                .output();
        }
    }
    #[cfg(target_os = "macos")]
    {
        for label in [AGENT_LABEL, COMPANION_LABEL] {
            let _ = std::process::Command::new("launchctl")
                .args([
                    "bootout",
                    &format!("gui/{}/{label}", current_uid().unwrap_or_default()),
                ])
                .output();
        }
    }

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

/// Open the companion's Preferences window via its IPC endpoint. If the
/// companion isn't running, start the service and retry with backoff.
#[tauri::command]
fn open_companion_preferences() -> Result<(), String> {
    if try_show_preferences_now().is_ok() {
        return Ok(());
    }

    // IPC listener binds a beat after the process launches.
    start_companion_service()?;

    let mut attempts_left = 15u32;
    let mut delay_ms = 150u64;
    loop {
        std::thread::sleep(std::time::Duration::from_millis(delay_ms));
        if try_show_preferences_now().is_ok() {
            return Ok(());
        }
        attempts_left = attempts_left.saturating_sub(1);
        if attempts_left == 0 {
            return Err(
                "Preferences window didn't open — companion may not have finished starting.".into(),
            );
        }
        delay_ms = (delay_ms + 100).min(600);
    }
}

fn try_show_preferences_now() -> Result<(), String> {
    match http_agent()
        .post(&format!(
            "http://127.0.0.1:{}/preferences/show",
            locai_link_shared::IPC_PORT
        ))
        .send_bytes(&[])
    {
        Ok(resp) if (200..300).contains(&resp.status()) => Ok(()),
        Ok(resp) => Err(format!("HTTP {} from IPC endpoint", resp.status())),
        Err(e) => Err(format!("IPC POST: {e}")),
    }
}

#[cfg(target_os = "linux")]
fn start_companion_service() -> Result<(), String> {
    let out = std::process::Command::new("systemctl")
        .args(["--user", "start", "locai-link-companion.service"])
        .output()
        .map_err(|e| format!("systemctl start: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "systemctl start failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn start_companion_service() -> Result<(), String> {
    let uid = current_uid()?;
    let out = std::process::Command::new("launchctl")
        .args(["kickstart", "-k", &format!("gui/{uid}/{COMPANION_LABEL}")])
        .output()
        .map_err(|e| format!("launchctl kickstart: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "launchctl kickstart failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(())
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn start_companion_service() -> Result<(), String> {
    Err("start_companion_service: unsupported platform".to_string())
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
            Ok(()) => eprintln!("[setup-assistant] device deregistered from Control"),
            Err(e) => eprintln!("[setup-assistant] deregister failed (continuing uninstall): {e}"),
        },
        None => eprintln!("[setup-assistant] no device identity found; skipping deregister"),
    }
}

/// Fire the uninstaller from the SA splash. `systemd-run --user --collect` on
/// Linux so the script survives the runtime + companion being killed mid-run.
#[tauri::command]
#[cfg(target_os = "linux")]
fn launch_uninstaller_from_sa(install_root: String) -> Result<(), String> {
    let script = format!("{install_root}/uninstall.sh");
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
fn launch_uninstaller_from_sa(_install_root: String) -> Result<(), String> {
    // Ignore the frontend-supplied install_root and resolve canonically — the
    // uninstaller runs with admin privileges, so no injectable path on that surface.
    let script = format!("{}/uninstall.sh", resolve_install_root());
    if !std::path::Path::new(&script).exists() {
        return Err(format!("uninstall.sh not found at {script}"));
    }
    // Deregister first: it needs the session api_key the uninstaller wipes. If
    // the spawn below fails, the device is deregistered but still installed
    // (recoverable on retry).
    best_effort_deregister();
    // AppleScript's `quoted form of` safely escapes the shell argument; `&`
    // concatenation keeps the path inside AppleScript's own string escaping.
    let escaped_path = script.replace('\\', "\\\\").replace('"', "\\\"");
    let apple_script = format!(
        "do shell script \"/bin/bash \" & quoted form of \"{escaped_path}\" with administrator privileges"
    );
    let out = std::process::Command::new("osascript")
        .args(["-e", &apple_script])
        .output()
        .map_err(|e| format!("osascript: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "uninstall.sh failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(())
}

#[tauri::command]
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn launch_uninstaller_from_sa(_install_root: String) -> Result<(), String> {
    Err("launch_uninstaller_from_sa: unsupported platform".to_string())
}

/// Exit the SA. `close()` on macOS just hides the window (Cocoa default);
/// this one-shot wizard needs the process to actually terminate.
#[tauri::command]
fn exit_app(app: tauri::AppHandle) {
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(SignInState::default())
        .invoke_handler(tauri::generate_handler![
            check_install,
            get_install_root,
            get_platform,
            supported_model_types,
            sign_in_start,
            sign_in_poll,
            suggest_device_name,
            list_models,
            deploy_model,
            mark_deployment_pending,
            wait_for_agent_ready,
            re_register,
            open_companion_preferences,
            launch_uninstaller_from_sa,
            mint_registration_key,
            register_device,
            install_agent_config,
            install_launchagents,
            exit_app,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
