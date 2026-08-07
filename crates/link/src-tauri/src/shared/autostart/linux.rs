// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Linux autostart stub.
//!
//! Real implementation writes a freedesktop.org autostart entry at
//! `~/.config/autostart/<app_id>.desktop` so any compliant desktop runs
//! `Exec=<exec_path>` once at login. Stub keeps [`crate::autostart`]'s API
//! stable until the Linux shipping target lands.

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
