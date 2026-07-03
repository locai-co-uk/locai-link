// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! macOS autostart via a per-user LaunchAgent under
//! `~/Library/LaunchAgents/<app_id>.plist`.
//!
//! Chose LaunchAgent (not Login Items via SMAppService) because:
//! * LaunchAgents work identically across the .pkg installer flow
//!   (postinstall drops the plist) and the developer flow (the app
//!   drops it on first launch) — same file, same effect.
//! * SMAppService requires code-signing entitlements and .app bundles
//!   registered with the framework; overkill for our surface.
//!
//! Mirrors the plist format that `link.infra.service::MacOSBackend`
//! writes for the agent, so both entries look uniform to the user
//! opening System Settings → General → Login Items.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;

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
    let exec_str = exec_path
        .to_str()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "exec_path is not valid UTF-8"))?;
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
    // Fire-and-forget: `launchctl load` returns non-zero when the label
    // is already loaded, which isn't a real error for our purposes.
    let _ = Command::new("launchctl")
        .args(["load", "-w"])
        .arg(&path)
        .status();
    Ok(())
}

pub fn disable(app_id: &str) -> io::Result<()> {
    let path = plist_path(app_id)?;
    // Unload first so launchd forgets the label; then remove the plist.
    // Ignore launchctl's exit code — the file removal is what matters.
    let _ = Command::new("launchctl")
        .args(["unload", "-w"])
        .arg(&path)
        .status();
    match fs::remove_file(&path) {
        Ok(()) => Ok(()),
        // Idempotent: already gone → success.
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
    }
}

pub fn is_enabled(app_id: &str) -> bool {
    plist_path(app_id).map(|p| p.exists()).unwrap_or(false)
}

/// `launchctl unload` on the plist without deleting the file — stops
/// the running instance but keeps the login-time hook intact.
///
/// If the plist doesn't exist, silently succeed: nothing to stop.
pub fn stop_now(app_id: &str) -> io::Result<()> {
    let path = plist_path(app_id)?;
    if !path.exists() {
        return Ok(());
    }
    // Fire-and-forget: launchctl reports non-zero for "already
    // unloaded" which isn't a real failure from the caller's
    // perspective. The caller (companion Quit) just wants best-effort
    // "stop everything now".
    let _ = Command::new("launchctl").args(["unload"]).arg(&path).status();
    Ok(())
}
