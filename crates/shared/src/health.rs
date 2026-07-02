// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Probe Link's `/healthz` endpoint. Consumed by the menu-bar app's
//! polling loop and by the Setup Assistant's "did the agent come up"
//! confirmation on Finish.

use std::time::Duration;

use serde::{Deserialize, Serialize};

/// Default endpoint the local agent exposes. Loopback-only by design;
/// see `src/link/infra/health_server.py`.
pub const DEFAULT_HEALTH_URL: &str = "http://127.0.0.1:8101/healthz";

/// Companion endpoint for listing servable-model pipelines. Same host
/// and port as `/healthz`.
pub const DEFAULT_MODELS_URL: &str = "http://127.0.0.1:8101/models";

/// Base URL for per-model action endpoints. Combined with
/// `/{pipeline_id}/{serve,stop-serving}` by [`toggle_serving`].
pub const DEFAULT_MODEL_ACTION_BASE: &str = "http://127.0.0.1:8101/models";

/// How long to wait for the agent to respond before treating it as Down.
/// Short — the menu-bar app polls on a UI cadence, and any real /healthz
/// call answers in milliseconds because it just returns a struct field.
const HEALTH_TIMEOUT: Duration = Duration::from_millis(2000);

/// Snapshot of the local agent's health, as reported by `/healthz`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentHealth {
    pub version: String,
    pub uptime_seconds: u64,
    pub currently_serving: bool,
    pub model_id: Option<String>,
}

/// One servable-model pipeline as reported by `/models`. Shape matches
/// `AgentRuntime._snapshot_models` in `src/link/app/runtime.py`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelInfo {
    /// Pipeline id — the stable handle used to target this model in
    /// future POST endpoints (e.g. `/models/{id}/serve`).
    pub id: String,
    /// Human-readable label from the pipeline config; falls back to
    /// `id` when no alias is set.
    pub alias: String,
    /// Port llama-swap would use if this model is (or would be)
    /// serving. `None` when the pipeline is entirely non-serving.
    pub port: Option<u16>,
    /// Loopback interface for the serving port. Effectively always
    /// `"127.0.0.1"` today but preserved for future non-loopback
    /// binds.
    pub host: String,
    /// `true` iff the pipeline is currently running AND its source
    /// has `mode=serve` (running-in-inference doesn't count).
    pub is_serving: bool,
}

/// Envelope returned by `/models`. One-key wrapper leaves room to
/// add sibling fields (e.g. `errors`, `warnings`) without a breaking
/// wire change.
#[derive(Debug, Clone, Deserialize)]
struct ModelsResponse {
    models: Vec<ModelInfo>,
}

/// Outcome of a single `/healthz` probe.
#[derive(Debug, Clone)]
pub enum HealthStatus {
    /// Agent responded successfully with a payload.
    Up(AgentHealth),
    /// Agent didn't respond (connection refused, timeout, etc.).
    Down,
    /// Agent responded but the payload didn't deserialise. Carries the raw error.
    Malformed(String),
}

/// Outcome of a single `/models` probe. Semantically parallel to
/// [`HealthStatus`] but for the model list.
#[derive(Debug, Clone)]
pub enum ModelsStatus {
    /// Endpoint responded and the payload deserialised.
    Ok(Vec<ModelInfo>),
    /// Endpoint didn't respond (timeout, connection refused, non-2xx).
    Down,
    /// Endpoint responded but the payload was unparseable.
    Malformed(String),
}

/// Fetch the servable-model list. Follows the same failure-mode
/// discipline as [`agent_health`] — nothing bubbles as `Result`; the
/// caller (polling loop) treats all failure modes the same way.
pub fn list_models(url: &str) -> ModelsStatus {
    let agent = ureq::AgentBuilder::new().timeout(HEALTH_TIMEOUT).build();
    match agent.get(url).call() {
        Ok(resp) => match resp.into_json::<ModelsResponse>() {
            Ok(body) => ModelsStatus::Ok(body.models),
            Err(e) => ModelsStatus::Malformed(e.to_string()),
        },
        Err(_) => ModelsStatus::Down,
    }
}

/// Whether a click on a model checkbox is intended to start serving or
/// stop serving. The Rust menu handler picks the variant based on the
/// model's current `is_serving` value.
#[derive(Copy, Clone, Debug)]
pub enum ServingAction {
    Start,
    Stop,
}

impl ServingAction {
    fn path(self) -> &'static str {
        match self {
            ServingAction::Start => "serve",
            ServingAction::Stop => "stop-serving",
        }
    }
}

/// Ask the agent to start or stop serving `pipeline_id`. Fire-and-
/// forget from the caller's perspective — success/failure is only
/// logged. The polling loop observes the effect on the next `/models`
/// tick, so a slow (or briefly-broken) POST never blocks the menu-bar
/// UI.
///
/// `base_url` is combined as `{base}/{pipeline_id}/{action}`. Use
/// [`DEFAULT_MODEL_ACTION_BASE`] as the base in production.
pub fn toggle_serving(base_url: &str, pipeline_id: &str, action: ServingAction) -> Result<(), String> {
    let url = format!("{base_url}/{pipeline_id}/{}", action.path());
    let agent = ureq::AgentBuilder::new().timeout(HEALTH_TIMEOUT).build();
    // POST with an empty body — the endpoint doesn't take one; the
    // pipeline's current args (port, host, alias) are read server-side
    // from its stored config.
    match agent.post(&url).send_bytes(&[]) {
        Ok(resp) if (200..300).contains(&resp.status()) => Ok(()),
        Ok(resp) => Err(format!("HTTP {} from {url}", resp.status())),
        Err(e) => Err(format!("POST {url} failed: {e}")),
    }
}

/// Probe Link's `/healthz` endpoint. Default URL is
/// [`DEFAULT_HEALTH_URL`]; callers can override for testing.
///
/// Any non-2xx response, timeout, connection refused, or malformed
/// payload maps to [`HealthStatus::Down`] or [`HealthStatus::Malformed`]
/// — never panics or bubbles up an error type, because the polling
/// loop on the tray icon just wants a snapshot.
pub fn agent_health(url: &str) -> HealthStatus {
    let agent = ureq::AgentBuilder::new().timeout(HEALTH_TIMEOUT).build();
    match agent.get(url).call() {
        Ok(resp) => match resp.into_json::<AgentHealth>() {
            Ok(payload) => HealthStatus::Up(payload),
            Err(e) => HealthStatus::Malformed(e.to_string()),
        },
        // Every failure mode — timeout, connection refused, non-2xx —
        // collapses to Down. The distinction only matters for logs, and
        // the polling loop can log the raw ureq error itself if it
        // wants to.
        Err(_) => HealthStatus::Down,
    }
}

#[cfg(test)]
mod tests {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;

    use super::*;

    #[test]
    fn agent_health_returns_down_on_connection_refused() {
        // Port 1 is the ICMP echo assignment, never has a listener.
        // Guaranteed connection-refused → Down, no timeout wait.
        let status = agent_health("http://127.0.0.1:1/healthz");
        assert!(matches!(status, HealthStatus::Down), "got {status:?}");
    }

    #[test]
    fn agent_health_up_on_valid_json() {
        let (port, handle) = serve_once(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 80\r\n\r\n\
             {\"version\":\"1.2.3\",\"uptime_seconds\":42,\"currently_serving\":true,\"model_id\":\"m1\"}",
        );
        let status = agent_health(&format!("http://127.0.0.1:{port}/healthz"));
        handle.join().unwrap();
        match status {
            HealthStatus::Up(h) => {
                assert_eq!(h.version, "1.2.3");
                assert_eq!(h.uptime_seconds, 42);
                assert!(h.currently_serving);
                assert_eq!(h.model_id.as_deref(), Some("m1"));
            }
            other => panic!("expected Up, got {other:?}"),
        }
    }

    #[test]
    fn agent_health_malformed_on_junk_body() {
        let (port, handle) = serve_once(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\n\r\nnot json{{",
        );
        let status = agent_health(&format!("http://127.0.0.1:{port}/healthz"));
        handle.join().unwrap();
        assert!(matches!(status, HealthStatus::Malformed(_)), "got {status:?}");
    }

    #[test]
    fn list_models_returns_down_on_connection_refused() {
        let status = list_models("http://127.0.0.1:1/models");
        assert!(matches!(status, ModelsStatus::Down), "got {status:?}");
    }

    #[test]
    fn list_models_ok_on_valid_json() {
        // Body: {"models":[{"id":"a","alias":"A","port":8080,"host":"127.0.0.1","is_serving":true},{"id":"b","alias":"B","port":null,"host":"127.0.0.1","is_serving":false}]}
        let body = "{\"models\":[{\"id\":\"a\",\"alias\":\"A\",\"port\":8080,\"host\":\"127.0.0.1\",\"is_serving\":true},{\"id\":\"b\",\"alias\":\"B\",\"port\":null,\"host\":\"127.0.0.1\",\"is_serving\":false}]}";
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
            body.len(),
            body
        );
        // serve_once takes a &'static str; leak the composed response
        // so the spawned thread can borrow it for the socket write.
        let (port, handle) = serve_once(Box::leak(response.into_boxed_str()));
        let status = list_models(&format!("http://127.0.0.1:{port}/models"));
        handle.join().unwrap();
        match status {
            ModelsStatus::Ok(models) => {
                assert_eq!(models.len(), 2);
                assert_eq!(models[0].id, "a");
                assert_eq!(models[0].port, Some(8080));
                assert!(models[0].is_serving);
                assert_eq!(models[1].id, "b");
                assert_eq!(models[1].port, None);
                assert!(!models[1].is_serving);
            }
            other => panic!("expected Ok, got {other:?}"),
        }
    }

    #[test]
    fn list_models_malformed_on_junk_body() {
        let (port, handle) = serve_once(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\n\r\nnot json{{",
        );
        let status = list_models(&format!("http://127.0.0.1:{port}/models"));
        handle.join().unwrap();
        assert!(matches!(status, ModelsStatus::Malformed(_)), "got {status:?}");
    }

    #[test]
    fn toggle_serving_hits_serve_path_and_reports_ok() {
        let (port, handle, captured) =
            serve_once_capturing("HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n");
        let res = toggle_serving(&format!("http://127.0.0.1:{port}/models"), "llm_server", ServingAction::Start);
        handle.join().unwrap();
        assert!(res.is_ok(), "got {res:?}");
        let request_line = String::from_utf8_lossy(&captured.lock().unwrap()).into_owned();
        assert!(
            request_line.starts_with("POST /models/llm_server/serve HTTP/1.1"),
            "got: {request_line:?}"
        );
    }

    #[test]
    fn toggle_serving_hits_stop_serving_path() {
        let (port, handle, captured) =
            serve_once_capturing("HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n");
        let res = toggle_serving(&format!("http://127.0.0.1:{port}/models"), "llm_server", ServingAction::Stop);
        handle.join().unwrap();
        assert!(res.is_ok(), "got {res:?}");
        let request_line = String::from_utf8_lossy(&captured.lock().unwrap()).into_owned();
        assert!(
            request_line.starts_with("POST /models/llm_server/stop-serving HTTP/1.1"),
            "got: {request_line:?}"
        );
    }

    #[test]
    fn toggle_serving_returns_err_on_non_2xx() {
        let (port, handle, _) = serve_once_capturing(
            "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n",
        );
        let res = toggle_serving(&format!("http://127.0.0.1:{port}/models"), "ghost", ServingAction::Start);
        handle.join().unwrap();
        assert!(res.is_err(), "expected Err on non-2xx, got {res:?}");
    }

    #[test]
    fn toggle_serving_returns_err_on_connection_refused() {
        let res = toggle_serving("http://127.0.0.1:1/models", "anything", ServingAction::Start);
        assert!(res.is_err(), "expected Err on refused connect, got {res:?}");
    }

    // Minimal one-shot HTTP server for the happy/malformed-path tests.
    // Binds to :0, reads the request bytes just enough to satisfy the
    // client, writes the canned response, then exits. Avoids a real
    // HTTP library dep for a two-test suite.
    fn serve_once(response: &'static str) -> (u16, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
        let port = listener.local_addr().unwrap().port();
        let handle = thread::spawn(move || {
            let (mut sock, _) = listener.accept().expect("accept");
            let mut buf = [0u8; 1024];
            let _ = sock.read(&mut buf);
            sock.write_all(response.as_bytes()).expect("write");
        });
        (port, handle)
    }

    // Same shape as `serve_once` but returns the captured request bytes
    // so the test can assert on the request line (method + path).
    fn serve_once_capturing(
        response: &'static str,
    ) -> (
        u16,
        thread::JoinHandle<()>,
        std::sync::Arc<std::sync::Mutex<Vec<u8>>>,
    ) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
        let port = listener.local_addr().unwrap().port();
        let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::<u8>::new()));
        let captured_for_thread = captured.clone();
        let handle = thread::spawn(move || {
            let (mut sock, _) = listener.accept().expect("accept");
            let mut buf = [0u8; 1024];
            let n = sock.read(&mut buf).unwrap_or(0);
            captured_for_thread.lock().unwrap().extend_from_slice(&buf[..n]);
            sock.write_all(response.as_bytes()).expect("write");
        });
        (port, handle, captured)
    }
}
