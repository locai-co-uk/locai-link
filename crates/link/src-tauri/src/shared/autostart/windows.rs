// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Windows autostart stub.
//!
//! Real implementation writes a REG_SZ under
//! `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, keyed on `app_id`
//! with the quoted `exec_path` as data, to run the exe once per logon. Stub
//! keeps [`crate::autostart`]'s API stable until the Windows target lands.

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
