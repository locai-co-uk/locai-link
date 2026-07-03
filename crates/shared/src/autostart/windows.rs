// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Windows autostart stub.
//!
//! Real implementation writes to
//! `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` — a REG_SZ
//! value keyed on `app_id` whose data is the quoted `exec_path`. That
//! runs the exe once per user session at logon.
//!
//! Lands when the Windows shipping target moves onto the roadmap; the
//! stub keeps [`crate::autostart`]'s API stable so the companion
//! doesn't grow a macOS-only autostart path in the meantime.

use std::io;
use std::path::Path;

pub fn enable(_app_id: &str, _exec_path: &Path) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "autostart::enable not yet implemented for Windows",
    ))
}

pub fn disable(_app_id: &str) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "autostart::disable not yet implemented for Windows",
    ))
}

pub fn is_enabled(_app_id: &str) -> bool {
    false
}

pub fn stop_now(_app_id: &str) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "autostart::stop_now not yet implemented for Windows",
    ))
}
