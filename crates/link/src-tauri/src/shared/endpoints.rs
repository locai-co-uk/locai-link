// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Cross-app endpoints and paths: the external service URLs, the app
//! <-> Setup Assistant IPC port, and the platform install root. Centralised so
//! the two Tauri apps don't each hardcode (and drift on) their own copies.

/// Control plane (dashboard) URL. Overridable at build time via
/// `LOCAI_CONTROL_URL` (dev builds); unset defaults to production.
pub const CONTROL_URL: &str = match option_env!("LOCAI_CONTROL_URL") {
    Some(url) => url,
    None => "https://control.locai.co.uk",
};

/// Workspace (chat) URL.
pub const WORKSPACE_URL: &str = "https://workspace.locai.co.uk";

/// Artifact-store base baked at build time via `LOCAI_ARTIFACT_BASE` (dev
/// builds). `None` leaves the runtime's own production default in force; the
/// supervisor hands a baked value to the runtime's environment on spawn.
pub const ARTIFACT_BASE: Option<&str> = option_env!("LOCAI_ARTIFACT_BASE");

/// Control API base baked at build time via `LOCAI_CONTROL_API_URL` (dev
/// builds). Same handoff as ARTIFACT_BASE: the supervisor exports it to the
/// runtime as LOCAI_API_URL so one-shot registration and the update check hit
/// the baked environment.
pub const CONTROL_API_BASE: Option<&str> = option_env!("LOCAI_CONTROL_API_URL");

/// Platform install root: macOS `/Library/Locai`, Linux `~/.local/share/locai`.
pub fn install_root() -> String {
    #[cfg(target_os = "macos")]
    {
        "/Library/Locai".to_string()
    }
    #[cfg(target_os = "linux")]
    {
        let home = std::env::var("HOME").unwrap_or_default();
        format!("{home}/.local/share/locai")
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        String::new()
    }
}
