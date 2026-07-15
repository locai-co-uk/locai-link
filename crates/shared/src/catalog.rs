// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Control device-API client for in-app model downloads (INFRA-343 / INFRA-362).
//!
//! Two device-authenticated endpoints, called with the device API key the
//! agent already holds (no user OAuth token in the app):
//!
//! - `GET  {api_url}/agent/{device_id}/available-models`
//! - `POST {api_url}/agent/{device_id}/models/{model_id}/request-deploy`
//!
//! Identity (`device_id`, `api_key`, `api_url`) is read from the newest
//! `session_*.json` under `<install_root>/configs`.

use std::path::Path;
use std::sync::LazyLock;
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// Control is remote, so this is looser than the loopback health timeout.
const CATALOG_TIMEOUT: Duration = Duration::from_secs(15);

static HTTP_AGENT: LazyLock<ureq::Agent> =
    LazyLock::new(|| ureq::AgentBuilder::new().timeout(CATALOG_TIMEOUT).build());

/// Device credentials pulled from the session config.
#[derive(Debug, Clone)]
pub struct DeviceIdentity {
    pub device_id: String,
    pub api_key: String,
    /// Control API base, e.g. `https://api.locai.co.uk/api/v1`.
    pub api_url: String,
}

/// One installable model, mirroring Control's `AvailableModelEntry`. Quantization
/// is not sent by Control; the UI derives it from `filename_on_server`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AvailableModel {
    pub model_id: String,
    pub display_name: String,
    pub framework: String,
    pub model_type: String,
    pub size_bytes: u64,
    pub filename_on_server: String,
    pub file_extension: String,
    pub is_globally_shared: bool,
    pub installed_on_device: bool,
}

#[derive(Debug, Clone, Deserialize)]
struct AvailableModelsResponse {
    models: Vec<AvailableModel>,
}

/// Result of a device-initiated deploy request. `command_id` is `None` when the
/// model was already installed; `status` is `dispatched`, `pending`, or
/// `already_installed`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeployOutcome {
    #[serde(default)]
    pub command_id: Option<String>,
    pub status: String,
}

/// Read `(device_id, api_key, api_url)` from the newest `session_*.json` under
/// `<install_root>/configs`. `None` when no session exists or a field is missing.
pub fn read_identity(install_root: &Path) -> Option<DeviceIdentity> {
    let configs = install_root.join("configs");
    let mut newest: Option<(std::time::SystemTime, std::path::PathBuf)> = None;
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
        // Tie-break equal mtimes by path: session_<UTC> filenames sort
        // chronologically, so selection stays deterministic regardless of
        // read_dir order.
        let candidate = (mtime, entry.path());
        if newest.as_ref().is_none_or(|best| candidate > *best) {
            newest = Some(candidate);
        }
    }
    let (_, path) = newest?;
    let body = std::fs::read_to_string(&path).ok()?;
    let json: serde_json::Value = serde_json::from_str(&body).ok()?;
    let identity = json.get("identity")?;
    let device_id = identity.get("device_id")?.as_str()?.to_string();
    let api_key = identity.get("api_key")?.as_str()?.to_string();
    let api_url = identity.get("api_url")?.as_str()?.to_string();
    if device_id.is_empty() || api_key.is_empty() || api_url.is_empty() {
        return None;
    }
    Some(DeviceIdentity {
        device_id,
        api_key,
        api_url,
    })
}

/// Percent-encode one URL path segment. RFC 3986 unreserved bytes pass through;
/// everything else becomes %XX, so a device_id/model_id containing /, ?, or #
/// can't alter the route.
fn encode_segment(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// List the device owner's installable models (owned plus globally shared).
pub fn list_available_models(id: &DeviceIdentity) -> Result<Vec<AvailableModel>, String> {
    let base = id.api_url.trim_end_matches('/');
    let url = format!(
        "{base}/agent/{}/available-models",
        encode_segment(&id.device_id)
    );
    let resp = HTTP_AGENT
        .get(&url)
        .set("Accept", "application/json")
        .set("Authorization", &format!("Bearer {}", id.api_key))
        .call()
        .map_err(|e| describe_err("list_available_models", e))?;
    let body: AvailableModelsResponse = resp
        .into_json()
        .map_err(|e| format!("list_available_models response malformed: {e}"))?;
    Ok(body.models)
}

/// Request a device-initiated deploy of `model_id`. Idempotent on Control's side.
pub fn request_deploy(id: &DeviceIdentity, model_id: &str) -> Result<DeployOutcome, String> {
    let base = id.api_url.trim_end_matches('/');
    let url = format!(
        "{base}/agent/{}/models/{}/request-deploy",
        encode_segment(&id.device_id),
        encode_segment(model_id)
    );
    let resp = HTTP_AGENT
        .post(&url)
        .set("Accept", "application/json")
        .set("Authorization", &format!("Bearer {}", id.api_key))
        // No request body; model + device are in the path.
        .send_bytes(&[])
        .map_err(|e| describe_err("request_deploy", e))?;
    resp.into_json()
        .map_err(|e| format!("request_deploy response malformed: {e}"))
}

/// Turn a ureq error into a display string, preserving the server's `detail`
/// body on non-2xx (ureq's default `Display` drops it).
fn describe_err(op: &str, err: ureq::Error) -> String {
    match err {
        ureq::Error::Status(code, resp) => match resp.into_string() {
            Ok(body) if !body.is_empty() => format!("{op} failed (HTTP {code}): {body}"),
            _ => format!("{op}: HTTP {code}"),
        },
        ureq::Error::Transport(t) => format!("{op} (transport): {t}"),
    }
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::*;

    #[test]
    fn read_identity_pulls_newest_session() {
        let dir = std::env::temp_dir().join(format!("locai-catalog-test-{}", std::process::id()));
        let configs = dir.join("configs");
        fs::create_dir_all(&configs).unwrap();
        fs::write(
            configs.join("session_20260101T000000Z.json"),
            r#"{"identity":{"device_id":"old","api_key":"k0","api_url":"https://api.example/api/v1"}}"#,
        )
        .unwrap();
        // Bump the second file's mtime so it wins regardless of write order.
        let newer = configs.join("session_20260202T000000Z.json");
        fs::write(
            &newer,
            r#"{"identity":{"device_id":"new","api_key":"k1","api_url":"https://api.example/api/v1/"}}"#,
        )
        .unwrap();
        let later = std::time::SystemTime::now() + Duration::from_secs(10);
        filetime_set(&newer, later);

        let id = read_identity(&dir).expect("identity");
        assert_eq!(id.device_id, "new");
        assert_eq!(id.api_key, "k1");
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_identity_none_when_field_missing() {
        let dir = std::env::temp_dir().join(format!("locai-catalog-miss-{}", std::process::id()));
        let configs = dir.join("configs");
        fs::create_dir_all(&configs).unwrap();
        fs::write(
            configs.join("session_20260101T000000Z.json"),
            r#"{"identity":{"device_id":"d","api_url":"https://api.example/api/v1"}}"#,
        )
        .unwrap();
        assert!(read_identity(&dir).is_none());
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_identity_none_when_no_configs_dir() {
        let dir = std::env::temp_dir().join(format!("locai-catalog-empty-{}", std::process::id()));
        assert!(read_identity(&dir).is_none());
    }

    #[test]
    fn read_identity_breaks_mtime_tie_by_filename() {
        let dir = std::env::temp_dir().join(format!("locai-catalog-tie-{}", std::process::id()));
        let configs = dir.join("configs");
        fs::create_dir_all(&configs).unwrap();
        let older = configs.join("session_20260101T000000Z.json");
        let newer = configs.join("session_20260202T000000Z.json");
        fs::write(
            &older,
            r#"{"identity":{"device_id":"older","api_key":"k","api_url":"https://x/api/v1"}}"#,
        )
        .unwrap();
        fs::write(
            &newer,
            r#"{"identity":{"device_id":"newer","api_key":"k","api_url":"https://x/api/v1"}}"#,
        )
        .unwrap();
        // Identical mtimes: the timestamped filename must decide.
        let t = std::time::UNIX_EPOCH + Duration::from_secs(1_800_000_000);
        filetime_set(&older, t);
        filetime_set(&newer, t);

        let id = read_identity(&dir).expect("identity");
        assert_eq!(id.device_id, "newer");
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn encode_segment_passes_unreserved_and_encodes_reserved() {
        assert_eq!(encode_segment("abc-123_XYZ.~"), "abc-123_XYZ.~");
        assert_eq!(encode_segment("dev-uuid-1234"), "dev-uuid-1234");
        assert_eq!(encode_segment("a/b?c#d"), "a%2Fb%3Fc%23d");
    }

    // Set mtime without pulling in a dep: reuse std by writing then touching.
    fn filetime_set(path: &Path, when: std::time::SystemTime) {
        // Best-effort; if the platform rejects it the test still asserts on
        // write-order which the harness preserves.
        let _ = std::fs::File::open(path).and_then(|f| f.set_modified(when));
    }
}
