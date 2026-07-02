// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Linux autostart stub.
//!
//! Real implementation writes a freedesktop.org autostart entry to
//! `~/.config/autostart/<app_id>.desktop`. That file is picked up by
//! any compliant desktop (GNOME, KDE, XFCE, Cinnamon…) at login and
//! results in `Exec=<exec_path>` being run once.
//!
//! Lands when the Linux shipping target moves onto the roadmap; the
//! stub keeps [`crate::autostart`]'s API stable so the companion
//! doesn't grow a macOS-only autostart path in the meantime.

use std::io;
use std::path::Path;

pub fn enable(_app_id: &str, _exec_path: &Path) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "autostart::enable not yet implemented for Linux",
    ))
}

pub fn disable(_app_id: &str) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "autostart::disable not yet implemented for Linux",
    ))
}

pub fn is_enabled(_app_id: &str) -> bool {
    false
}

pub fn stop_now(_app_id: &str) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "autostart::stop_now not yet implemented for Linux",
    ))
}
