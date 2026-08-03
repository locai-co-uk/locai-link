// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Structured status records emitted on stderr for the host application
//! to parse and render. Format: one JSON object per line, prefixed with
//! `LOCAI_STATUS:` so non-status log lines on the same stream get
//! ignored by the host's parser.
//!
//! Same channel is reused later by OTA updates from inside the runtime,
//! so the host's progress UI works for both bootstrap and update flows
//! through one integration.

use std::io::{self, Write};

use serde::Serialize;

const PREFIX: &str = "LOCAI_STATUS:";

#[derive(Serialize)]
#[serde(tag = "event", rename_all = "snake_case")]
// Variant names are the wire format (`event: bootstrap_*`); don't rename.
#[allow(clippy::enum_variant_names)]
pub enum Status<'a> {
    BootstrapStarted {
        stage: &'a str,
        asset: &'a str,
        size_total: Option<u64>,
    },
    BootstrapProgress {
        stage: &'a str,
        bytes_done: u64,
        bytes_total: Option<u64>,
    },
    BootstrapVerified {
        stage: &'a str,
        sha256: &'a str,
    },
    BootstrapExtracted {
        stage: &'a str,
        version: &'a str,
    },
    BootstrapReady {
        version: &'a str,
    },
    BootstrapFailed {
        stage: &'a str,
        error: &'a str,
    },
}

/// Emit a single status line on stderr. Errors writing are deliberately
/// swallowed — telemetry must never break the launcher.
pub fn emit(status: &Status<'_>) {
    let payload = match serde_json::to_string(status) {
        Ok(s) => s,
        Err(_) => return,
    };
    let mut stderr = io::stderr().lock();
    let _ = writeln!(stderr, "{PREFIX} {payload}");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn serializes_started() {
        let s = Status::BootstrapStarted {
            stage: "download",
            asset: "locai-link-llm-darwin-arm64.tar.gz",
            size_total: Some(200_000_000),
        };
        let json = serde_json::to_string(&s).unwrap();
        assert!(json.contains(r#""event":"bootstrap_started""#));
        assert!(json.contains(r#""stage":"download""#));
        assert!(json.contains(r#""size_total":200000000"#));
    }

    #[test]
    fn serializes_progress_without_total() {
        let s = Status::BootstrapProgress {
            stage: "download",
            bytes_done: 12345,
            bytes_total: None,
        };
        let json = serde_json::to_string(&s).unwrap();
        assert!(json.contains(r#""bytes_done":12345"#));
        assert!(json.contains(r#""bytes_total":null"#));
    }

    #[test]
    fn serializes_failed() {
        let s = Status::BootstrapFailed {
            stage: "verify",
            error: "sha256 mismatch",
        };
        let json = serde_json::to_string(&s).unwrap();
        assert!(json.contains(r#""event":"bootstrap_failed""#));
        assert!(json.contains(r#""stage":"verify""#));
        assert!(json.contains(r#""error":"sha256 mismatch""#));
    }
}
