// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Register a GUI app to launch when the user logs in.
//!
//! Distinct from `link.infra.service` (which registers the agent as a
//! background daemon) — a menu-bar app needs a user session with a
//! display server, so its autostart hook lives in a different place on
//! every platform:
//!
//! * macOS: LaunchAgent plist at `~/Library/LaunchAgents/<label>.plist`.
//! * Windows: registry value under
//!   `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
//! * Linux: freedesktop.org autostart entry at
//!   `~/.config/autostart/<name>.desktop`.
//!
//! [`enable`], [`disable`], and [`is_enabled`] present a single
//! platform-neutral surface so the menu-bar (and future GUI apps) never
//! import a platform-specific autostart module directly.

use std::io;
use std::path::Path;

#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "macos")]
mod macos;
#[cfg(target_os = "windows")]
mod windows;

/// Register `exec_path` to run at login under the identifier `app_id`.
///
/// * `app_id` is the reverse-DNS or file-safe stem the platform uses to
///   key the entry (e.g. `"uk.co.locai.link.companion"`).
/// * `exec_path` is the absolute path to the binary to launch. On
///   macOS this is typically the `.app` bundle's main executable; on
///   Windows the `.exe`; on Linux the packaged binary.
///
/// Idempotent — enabling an already-enabled entry replaces it in place.
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

/// Remove the login-time entry for `app_id`, if present.
///
/// Idempotent — disabling a missing entry is not an error.
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

/// Stop the currently-running instance of `app_id` without touching
/// its autostart config. Semantic difference from [`disable`]:
///
/// * [`disable`] removes the login-time hook AND stops the process
///   (registration is fully removed).
/// * [`stop_now`] only stops the process — the plist/registry entry
///   stays, so next login still auto-starts it.
///
/// Used by the companion's Quit action: the user wants to stop the
/// agent process right now, but keep autostart set so it comes back
/// on next login.
///
/// Idempotent — no error if nothing was running under `app_id`.
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

/// Reverse-DNS labels the .pkg installer uses when registering the
/// agent + companion LaunchAgents on macOS. Companion depends on
/// these to target the right plist from menu actions (autostart
/// toggle, Quit).
///
/// Deliberately not per-platform-conditional — the same labels serve
/// as the freedesktop autostart `.desktop` stem on Linux and the
/// `Run` registry value name on Windows once those implementations
/// land.
pub const AGENT_APP_ID: &str = "uk.co.locai.link.agent";
pub const COMPANION_APP_ID: &str = "uk.co.locai.link.companion";
