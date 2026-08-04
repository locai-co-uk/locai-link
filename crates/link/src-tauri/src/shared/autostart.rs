// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Register a GUI app to launch when the user logs in. Platform-neutral
//! wrapper around LaunchAgent plists (macOS), Run registry keys (Windows),
//! and freedesktop `.desktop` entries (Linux).

use std::io;
use std::path::Path;

#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "macos")]
mod macos;
#[cfg(target_os = "windows")]
mod windows;

/// Register `exec_path` to run at login under the identifier `app_id`. Idempotent.
pub fn enable(app_id: &str, exec_path: &Path) -> io::Result<()> {
    #[cfg(target_os = "macos")]
    {
        macos::enable(app_id, exec_path)
    }
    #[cfg(target_os = "windows")]
    {
        windows::enable(app_id, exec_path)
    }
    #[cfg(target_os = "linux")]
    {
        linux::enable(app_id, exec_path)
    }
    #[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
    {
        let _ = (app_id, exec_path);
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "autostart not implemented on this platform",
        ))
    }
}

/// Remove the login-time entry for `app_id`, if present. Idempotent.
pub fn disable(app_id: &str) -> io::Result<()> {
    #[cfg(target_os = "macos")]
    {
        macos::disable(app_id)
    }
    #[cfg(target_os = "windows")]
    {
        windows::disable(app_id)
    }
    #[cfg(target_os = "linux")]
    {
        linux::disable(app_id)
    }
    #[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
    {
        let _ = app_id;
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "autostart not implemented on this platform",
        ))
    }
}

/// Whether an autostart entry currently exists for `app_id`.
pub fn is_enabled(app_id: &str) -> bool {
    #[cfg(target_os = "macos")]
    {
        macos::is_enabled(app_id)
    }
    #[cfg(target_os = "windows")]
    {
        windows::is_enabled(app_id)
    }
    #[cfg(target_os = "linux")]
    {
        linux::is_enabled(app_id)
    }
    #[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
    {
        let _ = app_id;
        false
    }
}

/// Stop the running instance of `app_id` without removing its autostart
/// registration; the entry stays so next login re-launches it. Idempotent.
pub fn stop_now(app_id: &str) -> io::Result<()> {
    #[cfg(target_os = "macos")]
    {
        macos::stop_now(app_id)
    }
    #[cfg(target_os = "windows")]
    {
        windows::stop_now(app_id)
    }
    #[cfg(target_os = "linux")]
    {
        linux::stop_now(app_id)
    }
    #[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
    {
        let _ = app_id;
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "autostart not implemented on this platform",
        ))
    }
}

/// Reverse-DNS labels shared across platforms — LaunchAgent label on macOS,
/// `.desktop` stem on Linux, `Run` registry value name on Windows.
pub const AGENT_APP_ID: &str = "uk.co.locai.link.agent";
pub const COMPANION_APP_ID: &str = "uk.co.locai.link.companion";
