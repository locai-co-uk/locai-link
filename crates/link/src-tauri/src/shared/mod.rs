// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Platform + Control helpers (autostart, boot.json, install-state, health).
//! Internal library surface: not every item is used in every feature build
//! (most is `ui`-only), so `allow` the unused-in-this-config lints crate-wide
//! for this module — mirrors its prior life as a separate library crate.
#![allow(dead_code, unused_imports)]

pub mod autostart;
pub mod catalog;
pub mod endpoints;
pub mod health;
pub mod install;

pub use autostart::{AGENT_APP_ID, COMPANION_APP_ID};
pub use catalog::{
    deregister_device, list_available_models, read_identity, read_session_identity, request_deploy,
    AvailableModel, DeployOutcome, DeviceIdentity,
};
pub use endpoints::{install_root, CONTROL_URL, WORKSPACE_URL};
pub use health::{
    agent_health, cancel_deployment, list_models, mark_deployment_pending, toggle_serving,
    trigger_update, uninstall_model, AgentHealth, DeploymentProgress, HealthStatus, ModelInfo,
    ModelsStatus, ServingAction, TransportHealth, DEFAULT_HEALTH_URL, DEFAULT_MODELS_URL,
    DEFAULT_MODEL_ACTION_BASE, DEFAULT_PENDING_URL, DEFAULT_UPDATE_URL,
};
pub use install::{
    installed_version, read_boot_json, supported_model_types, BootConfig, InstalledVersion,
};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn types_compile() {
        fn _accepts_health(_: HealthStatus) {}
        fn _accepts_boot(_: BootConfig) {}
        fn _accepts_version(_: Option<InstalledVersion>) {}
    }
}
