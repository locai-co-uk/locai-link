// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Probe Link's `/healthz` and `/models` endpoints.

use std::sync::LazyLock;
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// Loopback-only endpoint the local agent exposes.
pub const DEFAULT_HEALTH_URL: &str = "http://127.0.0.1:50505/healthz";

/// Endpoint for listing servable-model pipelines.
pub const DEFAULT_MODELS_URL: &str = "http://127.0.0.1:50505/models";

/// Base for per-model action endpoints, combined with `/{pipeline_id}/{serve,stop-serving}`.
/// Same string as [`DEFAULT_MODELS_URL`] — kept distinct so call sites read as intent.
pub const DEFAULT_MODEL_ACTION_BASE: &str = DEFAULT_MODELS_URL;

const HEALTH_TIMEOUT: Duration = Duration::from_millis(2000);

/// Shared HTTP client so back-to-back polls reuse the connection pool.
static HTTP_AGENT: LazyLock<ureq::Agent> =
    LazyLock::new(|| ureq::AgentBuilder::new().timeout(HEALTH_TIMEOUT).build());

/// Snapshot of the local agent's health, as reported by `/healthz`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentHealth {
    pub version: String,
    pub uptime_seconds: u64,
    pub currently_serving: bool,
    pub model_id: Option<String>,
    /// `None` when the runtime is built without a transport, or on older
    /// runtimes that predate this field.
    #[serde(default)]
    pub transport: Option<TransportHealth>,
    /// In-flight model deployments. Empty when idle; older runtimes that
    /// predate this field parse as `Vec::new()`.
    #[serde(default)]
    pub deployments: Vec<DeploymentProgress>,
}

/// One in-flight model deployment. Removed from the list once completed on the runtime side.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct DeploymentProgress {
    /// Same handle as `ModelInfo::id`, joinable to overlay progress on a model row.
    pub pipeline_id: String,
    /// Asset file name. `None` on early ticks before the runtime resolves it.
    pub model_name: Option<String>,
    /// `downloading` or `configuring` — never `completed` on the wire.
    pub stage: String,
    /// 0.0–100.0. Throttled to 5% steps by the runtime's reporter.
    pub progress_pct: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TransportHealth {
    #[serde(rename = "type")]
    pub transport_type: String,
    pub endpoint: Option<String>,
    pub connected: bool,
}

/// One servable-model pipeline. Shape matches `AgentRuntime._snapshot_models`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelInfo {
    pub id: String,
    pub alias: String,
    /// `None` when the pipeline is entirely non-serving.
    pub port: Option<u16>,
    pub host: String,
    /// `true` iff the pipeline is running AND its source has `mode=serve`.
    pub is_serving: bool,
}

/// One-key envelope leaves room for sibling fields without a breaking wire change.
#[derive(Debug, Clone, Deserialize)]
struct ModelsResponse {
    models: Vec<ModelInfo>,
}

#[derive(Debug, Clone)]
pub enum HealthStatus {
    Up(AgentHealth),
    Down,
    Malformed(String),
}

#[derive(Debug, Clone)]
pub enum ModelsStatus {
    Ok(Vec<ModelInfo>),
    Down,
    Malformed(String),
}

/// Fetch the servable-model list. All failure modes collapse into `Down`/`Malformed` —
/// nothing bubbles as `Result`.
pub fn list_models(url: &str) -> ModelsStatus {
    match HTTP_AGENT.get(url).call() {
        Ok(resp) => match resp.into_json::<ModelsResponse>() {
            Ok(body) => ModelsStatus::Ok(body.models),
            Err(e) => ModelsStatus::Malformed(e.to_string()),
        },
        Err(_) => ModelsStatus::Down,
    }
}

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

/// Ask the agent to start or stop serving `pipeline_id`. `base_url` is combined
/// as `{base}/{pipeline_id}/{action}`.
pub fn toggle_serving(base_url: &str, pipeline_id: &str, action: ServingAction) -> Result<(), String> {
    let url = format!("{base_url}/{pipeline_id}/{}", action.path());
    // Empty body — endpoint reads pipeline args server-side.
    match HTTP_AGENT.post(&url).send_bytes(&[]) {
        Ok(resp) if (200..300).contains(&resp.status()) => Ok(()),
        Ok(resp) => Err(format!("HTTP {} from {url}", resp.status())),
        Err(e) => Err(format!("POST {url} failed: {e}")),
    }
}

/// Probe Link's `/healthz` endpoint. Every failure mode collapses to
/// `Down`/`Malformed` — never panics or bubbles a `Result`.
pub fn agent_health(url: &str) -> HealthStatus {
    match HTTP_AGENT.get(url).call() {
        Ok(resp) => match resp.into_json::<AgentHealth>() {
            Ok(payload) => HealthStatus::Up(payload),
            Err(e) => HealthStatus::Malformed(e.to_string()),
        },
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
        // Port 1 never has a listener — guaranteed connection-refused → Down.
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
        let body = "{\"models\":[{\"id\":\"a\",\"alias\":\"A\",\"port\":8080,\"host\":\"127.0.0.1\",\"is_serving\":true},{\"id\":\"b\",\"alias\":\"B\",\"port\":null,\"host\":\"127.0.0.1\",\"is_serving\":false}]}";
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
            body.len(),
            body
        );
        // serve_once takes a &'static str; leak the composed response for the write.
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

    // Minimal one-shot HTTP server: bind, accept once, write canned response, exit.
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

    // Same as `serve_once` but captures the request bytes for assertion.
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
