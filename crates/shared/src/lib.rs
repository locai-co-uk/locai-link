// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Helpers consumed by the launcher, the Setup Assistant, and the
//! menu-bar app. Three responsibilities:
//!
//! * [`agent_health`] — query Link's `/healthz` endpoint and return a
//!   typed snapshot of its state. The menu-bar app polls this to drive
//!   the green/grey indicator; the Setup Assistant calls it once on
//!   Finish to confirm the agent came up.
//! * [`read_boot_json`] — parse the `boot.json` config that the launcher
//!   reads on startup. Mirrored here so the Setup Assistant + menu-bar
//!   can read the same canonical record without re-implementing the
//!   schema.
//! * [`installed_version`] — resolve which version directory the
//!   `current` symlink points at under an `install_root`. Used by the
//!   menu-bar app's "About" surface and by the Setup Assistant's
//!   existing-install check.
//!
//! All three are stubbed for the scaffolding step; they return well-typed
//! `todo!()` placeholders so consumers can wire up signatures now and
//! the implementation can land in a follow-up.

use std::path::Path;

use serde::{Deserialize, Serialize};

/// Snapshot of the local agent's health, as reported by `/healthz`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentHealth {
    pub version: String,
    pub uptime_seconds: u64,
    pub currently_serving: bool,
    pub model_id: Option<String>,
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

/// Schema of the `boot.json` config the launcher reads on startup.
///
/// Mirrored from `launcher/src/boot.rs::BootConfig` — kept here so the
/// other Rust surfaces (Setup Assistant, menu-bar) can read the same
/// record without depending on the launcher crate directly.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BootConfig {
    pub host_app: Option<String>,
    pub plugin_set: Vec<String>,
    pub channel: String,
    pub asset_repo: String,
    pub asset_url: Option<String>,
}

/// Resolved currently-installed version of Link, as the launcher would see it.
#[derive(Debug, Clone)]
pub struct InstalledVersion {
    pub version: String,
    pub path: std::path::PathBuf,
}

/// Probe Link's `/healthz` endpoint. Default URL is
/// `http://127.0.0.1:8101/healthz`; callers can override for testing.
pub fn agent_health(_url: &str) -> HealthStatus {
    todo!("scaffold stub — implement with ureq once /healthz lands")
}

/// Parse a `boot.json` from disk.
pub fn read_boot_json(_path: &Path) -> Result<BootConfig, std::io::Error> {
    todo!("scaffold stub — read + deserialise once boot.json schema is final")
}

/// Resolve `<install_root>/current` to the version it points at.
pub fn installed_version(_install_root: &Path) -> Option<InstalledVersion> {
    todo!("scaffold stub — read the symlink + parse versions/<v>/ dir name")
}

#[cfg(test)]
mod tests {
    use super::*;

    // Smoke tests live here once the stubs become real implementations.
    // For the scaffolding step, we just assert the types exist and have
    // the expected shape so consumers can write against them.

    #[test]
    fn types_compile() {
        // If this builds, the three public types + the three function
        // signatures are stable enough for the Setup Assistant and
        // menu-bar app to depend on.
        fn _accepts_health(_: HealthStatus) {}
        fn _accepts_boot(_: BootConfig) {}
        fn _accepts_version(_: Option<InstalledVersion>) {}
    }
}
