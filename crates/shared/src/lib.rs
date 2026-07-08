// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Shared helpers used by the launcher, Setup Assistant, and menu-bar app.

pub mod autostart;
pub mod health;
pub mod install;

pub use health::{
    agent_health, list_models, toggle_serving, AgentHealth, DeploymentProgress, HealthStatus, ModelInfo, ModelsStatus, ServingAction, TransportHealth,
    DEFAULT_HEALTH_URL, DEFAULT_MODELS_URL, DEFAULT_MODEL_ACTION_BASE,
};
pub use autostart::{AGENT_APP_ID, COMPANION_APP_ID};
pub use install::{read_boot_json, BootConfig, InstalledVersion, installed_version};

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
