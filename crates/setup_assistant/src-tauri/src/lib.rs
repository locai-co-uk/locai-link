// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

use serde::Serialize;
use tauri::State;

use locai_link_shared::{installed_version, read_boot_json, BootConfig};

/// Prod Control API base. RFC 8628 device-flow endpoints hang off
/// `/auth/device/{code,token}` and device registration off `/devices/`.
///
/// TODO(env-config): swap to a build-time env selector so dev / staging
/// can point at `https://dev.api.locai.co.uk/api/v1` etc. Companion has
/// the same TODO on its `CONTROL_URL` const.
const CONTROL_API_URL: &str = "https://api.locai.co.uk/api/v1";

/// Timeout for both device-code initiation and each poll of the token
/// endpoint. Kept generous because the backend hits Firestore
/// synchronously on both paths.
const HTTP_TIMEOUT: Duration = Duration::from_secs(15);

/// User-Agent for outbound Control API calls. Google-fronted endpoints
/// have been observed to stall on requests without an explicit UA;
/// mirrors what the launcher does for GitHub Releases fetches.
const USER_AGENT: &str = "locai-link-setup-assistant/0.1.0";

/// Build an HTTP agent with the timeout + user-agent policy every
/// device-flow call needs. Cheap to construct; called per-command
/// rather than held in State to keep the module free of ureq types.
fn http_agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout(HTTP_TIMEOUT)
        .user_agent(USER_AGENT)
        .build()
}

/// Returns a device name derived from the machine's hostname. Matches
/// what Control shows for fleet-enrolled devices (which pass
/// `platform.node()` from the runtime) whenever the hostname alone is
/// usable; short hostnames like `"pc"` get suffixed with the OS name
/// because Control requires `Device.name` to be at least 3 chars.
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
        // Long enough to satisfy Control's `min_length=3` — use verbatim.
        Some(h) if h.chars().count() >= 3 => h,
        // Short but non-empty hostname (e.g. `pc`). Append the OS name
        // so the user still recognises their machine.
        Some(h) => format!("{h}-{}", std::env::consts::OS),
        // No hostname reported at all — fall back to a stable default
        // that still names the OS so the user has a hint.
        None => format!("locai-link-{}", std::env::consts::OS),
    }
}

/// Tauri command: get the hostname the SA should use when registering
/// this device. Called from the Finish step so the label matches what
/// fleet enrollment would produce for the same box.
#[tauri::command]
fn suggest_device_name() -> String {
    machine_hostname()
}

// --- Check Install -----------------------------------------------------------

/// Wire-format result for the Setup Assistant's "Check Install" step.
///
/// Kept flat and JSON-friendly so the Svelte side can consume it
/// without a schema library. `path` is a stringified `PathBuf` — the
/// frontend only shows it, never operates on it.
#[derive(Serialize)]
pub struct CheckInstallResult {
    pub installed: bool,
    pub version: Option<String>,
    pub path: Option<String>,
    pub boot: Option<BootConfig>,
    /// Populated when `boot.json` exists but couldn't be parsed. Kept
    /// distinct from `reason` so the UI can say "install found but
    /// config is broken" rather than hiding the failure.
    pub boot_error: Option<String>,
    /// Human-readable reason when `installed` is false. `None` on the
    /// success path.
    pub reason: Option<String>,
}

/// Read the on-disk install state at `install_root`.
///
/// Never returns an `Err` — the failure modes ("no install here",
/// "install root doesn't exist", "boot.json corrupt") are legitimate
/// outcomes the UI needs to render, not exceptions. `reason` /
/// `boot_error` carry the detail.
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

    match installed_version(&root) {
        Some(v) => CheckInstallResult {
            installed: true,
            version: Some(v.version),
            path: Some(v.path.to_string_lossy().into_owned()),
            boot,
            boot_error,
            reason: None,
        },
        None => CheckInstallResult {
            installed: false,
            version: None,
            path: None,
            boot,
            boot_error,
            reason: Some("No `current` pointer found under install root.".to_string()),
        },
    }
}

// --- Sign in (RFC 8628 device authorization) --------------------------------

/// Response to the front-end's `sign_in_start` invocation. Mirrors the
/// backend's `DeviceCodeResponse` (see platform_backend
/// `user_routes.py::DeviceCodeResponse`) minus the `device_code` — the
/// front-end never needs the raw device code, it lives in `SignInState`
/// and the poll command reads it from there.
#[derive(Serialize)]
pub struct DeviceCodeStart {
    pub user_code: String,
    pub verification_uri: String,
    pub verification_uri_complete: String,
    pub interval: u64,
    pub expires_in: u64,
}

/// Wire result of a single poll. Every RFC §3.5 error case becomes a
/// distinct variant so the front-end doesn't have to inspect strings.
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
    /// Anything the front-end can't recover from — network failure,
    /// unexpected status, missing fields. Message is for display.
    Error {
        message: String,
    },
}

/// Backend-issued session held server-side (i.e. in the Rust process).
/// The JWT never crosses the Tauri IPC boundary — the front-end can
/// only ask "am I signed in" and "please make an authenticated call".
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

/// Kick off RFC 8628 device authorization. Stores the device_code
/// server-side so subsequent polls don't require it as an arg.
///
/// Retries once on transport errors (timeout / connection refused).
/// The first hit to `/auth/device/code` after boot has been observed
/// to time out on macOS — likely DNS/TLS cold start on the fresh
/// process's network stack — while an immediate retry succeeds. A
/// single-shot retry with a short pause covers that without pushing
/// the wizard's total wait past a reasonable budget. Status errors
/// (4xx/5xx from Control) skip the retry — those are the server
/// saying "no" and won't change on repeat.
#[tauri::command]
fn sign_in_start(state: State<'_, SignInState>) -> Result<DeviceCodeStart, String> {
    let payload = serde_json::json!({
        "client_metadata": {
            "os": std::env::consts::OS,
            "source": "setup_assistant",
        }
    });

    let send = || {
        http_agent()
            .post(&format!("{CONTROL_API_URL}/auth/device/code"))
            .set("Accept", "application/json")
            .send_json(payload.clone())
    };
    let resp = match send() {
        Ok(r) => r,
        Err(ureq::Error::Transport(t)) => {
            // Log the first-attempt failure so we can diagnose if the
            // retry also fails. Tauri routes this to the SA's stderr;
            // shows up alongside `install_launchagents` logs on macOS.
            eprintln!("sign_in_start: first attempt failed (transport: {t}); retrying once");
            std::thread::sleep(std::time::Duration::from_millis(1500));
            send().map_err(|e| describe_ureq_err("device code request", e))?
        }
        Err(e) => return Err(describe_ureq_err("device code request", e)),
    };

    let body: serde_json::Value = resp
        .into_json()
        .map_err(|e| format!("device code response malformed: {e}"))?;

    let device_code = body
        .get("device_code")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "device_code missing from response".to_string())?
        .to_string();
    let user_code = body
        .get("user_code")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "user_code missing from response".to_string())?
        .to_string();
    let verification_uri = body
        .get("verification_uri")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "verification_uri missing from response".to_string())?
        .to_string();
    let verification_uri_complete = body
        .get("verification_uri_complete")
        .and_then(|v| v.as_str())
        .unwrap_or(&verification_uri)
        .to_string();
    let interval = body.get("interval").and_then(|v| v.as_u64()).unwrap_or(5);
    let expires_in = body.get("expires_in").and_then(|v| v.as_u64()).unwrap_or(600);

    *state.inner.lock().expect("SignInState poisoned") = Some(Session {
        device_code,
        access_token: None,
        refresh_token: None,
        user_id: None,
        email: None,
    });

    Ok(DeviceCodeStart {
        user_code,
        verification_uri,
        verification_uri_complete,
        interval,
        expires_in,
    })
}

/// Poll the token endpoint once. The Svelte side is expected to space
/// polls by the `interval` returned from `sign_in_start` (and bump it on
/// `SlowDown`).
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
            let body: serde_json::Value = match resp.into_json() {
                Ok(v) => v,
                Err(e) => {
                    return SignInPollResult::Error {
                        message: format!("token response malformed: {e}"),
                    }
                }
            };
            let access_token = body
                .get("access_token")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let refresh_token = body
                .get("refresh_token")
                .and_then(|v| v.as_str())
                .map(String::from);
            let user = body.get("user").cloned().unwrap_or_default();
            let user_id = user
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let email = user
                .get("email")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let username = user
                .get("username")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();

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
        // RFC §3.5 non-success codes: HTTP 400 with `detail.error` set.
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
        // Control-plane 429 (rate-limit), not RFC slow_down. Treat as
        // slow_down so the front-end backs off the same way.
        Err(ureq::Error::Status(429, _)) => SignInPollResult::SlowDown,
        Err(e) => SignInPollResult::Error {
            message: format!("token poll failed: {e}"),
        },
    }
}

// --- Register device against Control -----------------------------------------

/// Wire result of registering the device with Control. Mirrors
/// `RegisterWithKeyResponse` on the backend
/// (`platform_backend/.../device_routes.py::RegisterWithKeyResponse`).
/// `config` is the AgentConfig blob the runtime expects to see on
/// disk; we hand it back to the front-end so the Finish step can pass
/// it to `install_agent_config` verbatim.
#[derive(Serialize)]
pub struct RegisteredDevice {
    pub device_id: String,
    pub api_key: String,
    pub config: serde_json::Value,
}

/// Mints a fresh single-use registration key using the stored JWT,
/// then registers `device_name` against it. Returns the device id +
/// api_key + AgentConfig. Errors are `String` so they surface to the
/// UI as toasts.
///
/// Fails fast if the user never signed in — the JWT is required to
/// authenticate both calls.
#[tauri::command]
fn register_device(
    state: State<'_, SignInState>,
    device_name: String,
) -> Result<RegisteredDevice, String> {
    let token = require_token(&state)?;

    // 1) Mint a registration key. `registration_source=onboarding_wizard`
    //    threads through to Control's activation-funnel analytics.
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

    // 2) Redeem the key for a device_id + api_key + AgentConfig.
    let register_body = serde_json::json!({
        "registration_key": registration_key,
        "name": device_name,
        "device_type": "other",
        "metadata": {
            "os": std::env::consts::OS,
            "arch": std::env::consts::ARCH,
            "source": "setup_assistant",
        },
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
    let config = reg_body.get("config").cloned().unwrap_or(serde_json::Value::Null);

    Ok(RegisteredDevice {
        device_id,
        api_key,
        config,
    })
}

/// Write `config` into `<install_root>/configs/session_YYYYMMDD_HHMMSS.json`
/// — the location the runtime's `StateManager` picks up on next start
/// (`src/link/app/state.py`, `STATE_DIR = Path("configs")` resolved
/// against the LaunchAgent's WorkingDirectory `/Library/Locai`).
/// Returns the path written for UI display.
#[tauri::command]
fn install_agent_config(
    install_root: String,
    config: serde_json::Value,
) -> Result<String, String> {
    if config.is_null() {
        return Err("config from register_device was null — nothing to write".to_string());
    }

    // Resolve `${identity.<field>}` placeholders in the same way
    // `_apply_server_config` does in src/link/app/onboarding.py. The
    // backend ships topic strings like
    // "locai/devices/${identity.device_id}/metrics" with the
    // placeholder unfilled. If we write those verbatim the runtime
    // publishes/subscribes to literal paths containing "${...}" —
    // Control never sees telemetry or lifecycle events, and the
    // device stays "version unknown" forever.
    //
    // Uses the identity block that's already resolved in the config
    // itself as the substitution context, matching the Python
    // resolve_templates() semantics: unknown placeholders (e.g. the
    // runtime's `{cid}` / `{mid}` per-emit markers) pass through
    // untouched.
    let identity = config.get("identity").cloned().unwrap_or_default();
    let context = serde_json::json!({ "identity": identity });
    let config = resolve_config_templates(&config, &context);

    let root = PathBuf::from(&install_root);
    if !root.exists() {
        return Err(format!(
            "install root not found at {}. Is Loc.ai Link installed?",
            root.display()
        ));
    }
    // configs/ is normally created by the runtime on first start
    // (see StateManager.__init__ in src/link/app/state.py). On a
    // fresh .pkg install the runtime hasn't run yet, so the dir
    // won't exist — create it ourselves rather than error out. Not
    // inside `current/`: `current/` is the versioned code symlink
    // and OTA flips it; session state must survive version updates.
    let configs_dir = root.join("configs");
    std::fs::create_dir_all(&configs_dir).map_err(|e| {
        format!(
            "create configs dir {}: {e}",
            configs_dir.display()
        )
    })?;

    // Filename mirrors StateManager.bootstrap()'s format: session_<UTC>.json.
    // Uses UTC so two SA runs on different machines produce sortable names.
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| format!("system clock: {e}"))?;
    let secs = now.as_secs();
    // Minimal YYYYMMDD_HHMMSS formatter — no chrono dep needed.
    let ts = format_utc_compact(secs);
    let session_path = configs_dir.join(format!("session_{ts}.json"));

    let serialized = serde_json::to_string_pretty(&config)
        .map_err(|e| format!("serialise config: {e}"))?;
    std::fs::write(&session_path, serialized)
        .map_err(|e| format!("write {}: {e}", session_path.display()))?;

    // Match StateManager._tighten_permissions on Unix — the session
    // file contains the device api_key.
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

/// Wire-format subset of the backend's `ModelResponse` — only the
/// fields the wizard actually renders. Skips the heavyweight layer /
/// summary blobs that `/models/list_without_layers_info` already
/// omits.
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

/// Turns a ureq error into a `String` suitable for `Result<T, String>`
/// return to the front-end, preserving the server's `detail` field on
/// non-2xx. Without this, ureq's default Display just prints
/// `"<url>: status code <n>"` and swallows the JSON body — which
/// is exactly the field Control uses to say *why* it rejected the
/// call (e.g. `"detail": "device name too long"`).
fn describe_ureq_err(op: &str, err: ureq::Error) -> String {
    match err {
        ureq::Error::Status(code, resp) => {
            let url = resp.get_url().to_string();
            // .into_string() consumes the response body. Take a best-
            // effort look; on failure we still return a useful
            // status-only message.
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

/// Reads the JWT out of `SignInState`. Every JWT-authed command shares
/// this failure mode ("call sign_in first"), so having one helper
/// avoids repeating the lock/unwrap dance and gives the front-end a
/// consistent error string.
fn require_token(state: &State<'_, SignInState>) -> Result<String, String> {
    state
        .inner
        .lock()
        .expect("SignInState poisoned")
        .as_ref()
        .and_then(|s| s.access_token.clone())
        .ok_or_else(|| "not signed in — sign in first".to_string())
}

/// Lists the models available to the signed-in user (including
/// shared/org-visible ones). Hits `/models/list_without_layers_info`
/// so we skip the ~MB of per-layer detail the SA has no use for.
#[tauri::command]
fn list_models(state: State<'_, SignInState>) -> Result<Vec<ModelSummary>, String> {
    let token = require_token(&state)?;

    let resp = http_agent()
        .get(&format!("{CONTROL_API_URL}/models/list_without_layers_info"))
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

    // Skip any entry missing an id or display_name — the UI has no
    // sensible way to render them and Control shouldn't be sending
    // them, so silent drop is fine.
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

/// Queue a deploy of `model_id` onto `device_id`. Returns the
/// deployment id. This just enqueues a command server-side (Control
/// dispatches it via Zenoh); the runtime does the actual download when
/// it receives it, so this call returns fast regardless of model size.
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
        // Endpoint takes no body — path params only. Send an explicit
        // empty payload rather than `.call()`: `.call()` on a POST
        // doesn't set `Content-Length`, and Google's L7 load balancer
        // in front of Control rejects headerless POSTs with `HTTP 411
        // Length Required` before the request ever reaches the API.
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

/// Recursively substitute `${path.to.key}` placeholders in a JSON
/// value using dotted lookups against `context`. Mirrors the semantics
/// of the runtime's `resolve_templates` in
/// `src/link/config/templating.py`: dicts and arrays are walked;
/// strings get placeholders substituted; unknown placeholders are
/// preserved verbatim so per-emit markers (e.g. `{cid}` / `{mid}` from
/// the reporting handlers) pass through untouched.
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
                    // Unknown placeholder — preserve verbatim (this
                    // is how `{cid}` / `{mid}` and any other unknown
                    // key survive to be substituted later by the
                    // runtime's per-emit handlers).
                    None => result.push_str(&rest[open..open + 2 + close + 1]),
                }
                rest = &after_open[close + 1..];
            }
            None => {
                // Unclosed "${" — copy the rest verbatim, stop scanning.
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
        // Preserve the placeholder rather than emit "null" verbatim.
        serde_json::Value::Null => None,
        // String needs .clone() — .to_string() on a JSON string wraps
        // it in escaped quotes.
        serde_json::Value::String(s) => Some(s.clone()),
        // Numbers / bools stringify cleanly via Display.
        other => Some(other.to_string()),
    }
}

// --- LaunchAgent bootstrap (macOS) -------------------------------------------

/// Copy the runtime + companion LaunchAgent plists from
/// `<install_root>/LaunchAgents/` into the user's
/// `~/Library/LaunchAgents/`, then bootstrap + kickstart both.
///
/// `run_at_login` controls only the plist's `RunAtLoad` key. Both
/// plists are always installed and bootstrapped so `launchctl kickstart`
/// from the companion works either way. When the toggle is off:
///   * `RunAtLoad` in each plist is patched to `false` before bootstrap
///   * both agents are still kickstarted now (so the user sees the tray
///     icon + runtime come up right after Finish — the setup they just
///     completed pays off immediately)
///   * next login, launchd sees RunAtLoad=false and leaves them alone
///
/// Users can also flip this later from System Settings → General →
/// Login Items (the LaunchAgents appear there as "background items").
///
/// No-op on non-macOS platforms — `launchctl` doesn't exist elsewhere.
/// The install path branches on target_os so Linux dev machines don't
/// error out running through the flow.
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
    std::fs::create_dir_all(&dest_dir).map_err(|e| {
        format!("create {}: {e}", dest_dir.display())
    })?;

    // Names + labels must match the plists in bundling/pkg/LaunchAgents/.
    let agents: [(&str, &str); 2] = [
        (
            "uk.co.locai.link.agent.plist",
            "uk.co.locai.link.agent",
        ),
        (
            "uk.co.locai.link.companion.plist",
            "uk.co.locai.link.companion",
        ),
    ];

    for (plist_name, label) in agents {
        let src = source_dir.join(plist_name);
        let dst = dest_dir.join(plist_name);
        std::fs::copy(&src, &dst)
            .map_err(|e| format!("copy {} -> {}: {e}", src.display(), dst.display()))?;

        // Toggle RunAtLoad via PlistBuddy — macOS-native, handles
        // xml/binary plists equally, and doesn't depend on the exact
        // whitespace of the source file the way a string replace would.
        // Source plists ship with RunAtLoad=true; only touch when the
        // user opted out.
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

        // Bootstrap into the user's GUI domain (aqua session). If the
        // agent is already loaded from a prior run, bootstrap returns
        // an error we can ignore — kickstart below still refreshes
        // the running process.
        let uid = current_uid()?;
        let domain = format!("gui/{uid}");
        let _ = std::process::Command::new("launchctl")
            .args(["bootstrap", &domain, dst.to_str().unwrap_or("")])
            .output();

        // kickstart -k restarts the agent if it was already running,
        // or starts it fresh if it wasn't. Either way the user sees
        // it come up now, not on next login.
        let service = format!("{domain}/{label}");
        let _ = std::process::Command::new("launchctl")
            .args(["kickstart", "-k", &service])
            .output();
    }

    Ok(())
}

#[tauri::command]
#[cfg(not(target_os = "macos"))]
fn install_launchagents(_install_root: String, _run_at_login: bool) -> Result<(), String> {
    // launchctl doesn't exist off macOS. The SA is designed for the
    // macOS .pkg flow; Linux dev sessions running the wizard just
    // skip this step so the rest of the flow still exercises.
    Ok(())
}

/// Get the current user's UID by shelling out to `id -u`. Avoids
/// pulling `libc` into an otherwise-libc-free crate. macOS ships
/// `id` in /usr/bin.
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

/// UTC seconds → `YYYYMMDD_HHMMSS` — enough date arithmetic to avoid
/// bringing chrono into a Tauri app that otherwise doesn't need it.
fn format_utc_compact(unix_secs: u64) -> String {
    // Days since 1970-01-01, remainder is time-of-day.
    let days = unix_secs / 86_400;
    let tod = unix_secs % 86_400;
    let h = tod / 3600;
    let m = (tod % 3600) / 60;
    let s = tod % 60;

    // Civil-from-days (Howard Hinnant). Handles 1970..∞ without pulling
    // a date library. See http://howardhinnant.github.io/date_algorithms.html.
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
            sign_in_start,
            sign_in_poll,
            suggest_device_name,
            list_models,
            deploy_model,
            register_device,
            install_agent_config,
            install_launchagents,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
