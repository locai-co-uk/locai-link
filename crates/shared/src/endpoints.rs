// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Cross-app endpoints and paths: the external service URLs, the companion
//! <-> Setup Assistant IPC port, and the platform install root. Centralised so
//! the two Tauri apps don't each hardcode (and drift on) their own copies.

/// Control plane (dashboard) URL.
pub const CONTROL_URL: &str = "https://control.locai.co.uk";

/// Workspace (chat) URL.
pub const WORKSPACE_URL: &str = "https://workspace.locai.co.uk";

/// Companion IPC port, adjacent to the agent health server's 20505. The
/// companion listens here; the Setup Assistant posts to it.
pub const IPC_PORT: u16 = 20506;

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
