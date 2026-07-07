// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Helpers consumed by the launcher, the Setup Assistant, and the
//! menu-bar app.
//!
//! Grouped by responsibility:
//!
//! * [`health`] — probe Link's `/healthz` endpoint. Real HTTP call with
//!   a short timeout; drives the menu-bar tray indicator.
//! * [`install`] — read the on-disk install state (boot.json + current
//!   version pointer). Still stubbed (`Err(NotFound)`, `None`); real
//!   parser lands with the Setup Assistant's existing-install check.
//! * [`autostart`] — register a GUI app to launch at login.
//!   Cross-platform surface with a real macOS implementation and
//!   Windows/Linux stubs.

pub mod autostart;
pub mod health;
pub mod install;

// Flat re-exports keep the public surface identical to the pre-split
// crate, so downstream callers (`companion`, `setup_assistant`,
// `launcher`) don't need to update their imports for the reorganisation.
pub use health::{
    agent_health, list_models, toggle_serving, AgentHealth, HealthStatus, ModelInfo, ModelsStatus, ServingAction, TransportHealth,
    DEFAULT_HEALTH_URL, DEFAULT_MODELS_URL, DEFAULT_MODEL_ACTION_BASE,
};
pub use autostart::{AGENT_APP_ID, COMPANION_APP_ID};
pub use install::{read_boot_json, BootConfig, InstalledVersion, installed_version};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn types_compile() {
        // Signature stability check — if this builds, the public
        // surface is stable enough for the Setup Assistant and
        // menu-bar app to depend on.
        fn _accepts_health(_: HealthStatus) {}
        fn _accepts_boot(_: BootConfig) {}
        fn _accepts_version(_: Option<InstalledVersion>) {}
    }
}
