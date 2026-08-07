// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! macOS autostart via a per-user LaunchAgent under
//! `~/Library/LaunchAgents/<app_id>.plist`.
//!
//! LaunchAgent (not SMAppService Login Items): one plist works for both the
//! .pkg installer and developer flows, avoiding SMAppService's code-signing
//! entitlement and registered-.app requirements.
//!
//! Matches the plist format `link.infra.service::MacOSBackend` writes for the
//! agent, so both entries look uniform in System Settings Login Items.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;

/// Escape the five XML predefined entities so a value with `&`, `<`, etc. (e.g.
/// an install path containing `&`) can't produce a malformed plist.
fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

/// Path to the plist file for a given app_id under the user's LaunchAgents dir.
fn plist_path(app_id: &str) -> io::Result<PathBuf> {
    let home = std::env::var_os("HOME")
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "HOME not set"))?;
    Ok(PathBuf::from(home)
        .join("Library")
        .join("LaunchAgents")
        .join(format!("{app_id}.plist")))
}

pub fn enable(app_id: &str, exec_path: &Path) -> io::Result<()> {
    let path = plist_path(app_id)?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let exec_str = exec_path.to_str().ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidInput, "exec_path is not valid UTF-8")
    })?;
    let app_id = xml_escape(app_id);
    let exec_str = xml_escape(exec_str);
    let plist = format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{app_id}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exec_str}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
"#
    );
    fs::write(&path, plist)?;
    // Fire-and-forget: `launchctl load` returns non-zero for an already-loaded label.
    let _ = Command::new("launchctl")
        .args(["load", "-w"])
        .arg(&path)
        .status();
    Ok(())
}

pub fn disable(app_id: &str) -> io::Result<()> {
    let path = plist_path(app_id)?;
    // Unload so launchd forgets the label, then remove the plist. The file
    // removal is what matters, so ignore launchctl's exit code.
    let _ = Command::new("launchctl")
        .args(["unload", "-w"])
        .arg(&path)
        .status();
    match fs::remove_file(&path) {
        Ok(()) => Ok(()),
        // Idempotent: already gone counts as success.
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
    }
}

pub fn is_enabled(app_id: &str) -> bool {
    plist_path(app_id).map(|p| p.exists()).unwrap_or(false)
}

/// `launchctl unload` on the plist without deleting the file: stops the
/// running instance but keeps the login-time hook intact. Succeeds silently
/// when the plist doesn't exist.
pub fn stop_now(app_id: &str) -> io::Result<()> {
    let path = plist_path(app_id)?;
    if !path.exists() {
        return Ok(());
    }
    // Fire-and-forget: launchctl reports non-zero for an already-unloaded label,
    // and the caller only wants best-effort "stop everything now".
    let _ = Command::new("launchctl")
        .args(["unload"])
        .arg(&path)
        .status();
    Ok(())
}
