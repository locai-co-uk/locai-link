// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Device lifecycle: deregister, service control (start/stop/restart), and
//! uninstall. One implementation for both shapes — the desktop build is the
//! headless supervisor plus a UI, so these operations are identical and differ
//! only in the service label. Built on the shared `read_identity` /
//! `deregister_device` primitives so nothing is reimplemented per shape.
//!
//! Both shapes run under the user's service manager (systemd `--user` on Linux,
//! `launchctl gui/<uid>` on macOS), so all control here is user-scoped.

use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

use crate::shared::{deregister_device, read_identity};

/// A per-user service unit, identified per platform.
pub struct ServiceUnit {
    /// Reverse-DNS label: the launchctl target and the LaunchAgent plist stem.
    pub label: &'static str,
    /// The systemd unit filename on Linux.
    pub unit: &'static str,
}

/// The unit this build controls: the desktop companion under `ui`, else headless.
pub fn shape_unit() -> ServiceUnit {
    #[cfg(feature = "ui")]
    {
        ServiceUnit {
            label: "uk.co.locai.link.companion",
            unit: "locai-link-companion.service",
        }
    }
    #[cfg(not(feature = "ui"))]
    {
        // Linux systemd uses the kebab unit name (parity with the desktop
        // `locai-link-companion.service`); the reverse-DNS label is the macOS
        // launchd target.
        ServiceUnit {
            label: "uk.co.locai.link.headless",
            unit: "locai-link-headless.service",
        }
    }
}

/// The install root: `LOCAI_INSTALL_ROOT`, else the running binary's directory
/// (the flat headless layout puts `locai-link` at the root top).
fn install_root() -> PathBuf {
    if let Some(root) = std::env::var_os("LOCAI_INSTALL_ROOT") {
        return PathBuf::from(root);
    }
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Best-effort device delete from Control. Never fatal: a 404 (already gone) is
/// success; any other error is logged and swallowed so uninstall still proceeds.
pub fn deregister(root: &Path) {
    match read_identity(root) {
        Some(id) => match deregister_device(&id) {
            Ok(()) => eprintln!("Deregistered from Control."),
            Err(e) => eprintln!("Could not deregister from Control (continuing): {e}"),
        },
        None => eprintln!("No device identity found; skipping deregister."),
    }
}

// ---- service control ------------------------------------------------------

#[cfg(target_os = "macos")]
fn current_uid() -> String {
    Command::new("id")
        .arg("-u")
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
}

fn run_ok(program: &str, args: &[&str]) -> Result<(), String> {
    let out = Command::new(program)
        .args(args)
        .output()
        .map_err(|e| format!("{program} {args:?}: {e}"))?;
    if out.status.success() {
        Ok(())
    } else {
        Err(format!(
            "{program} {args:?} failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ))
    }
}

pub fn service_start(u: &ServiceUnit) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    {
        run_ok("systemctl", &["--user", "start", u.unit])
    }
    #[cfg(target_os = "macos")]
    {
        let plist = plist_path(u);
        run_ok("launchctl", &["bootstrap", &format!("gui/{}", current_uid()), &plist])
    }
    #[cfg(target_os = "windows")]
    {
        run_ok("schtasks", &["/Run", "/TN", u.label])
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
    {
        let _ = u;
        Err("service control not supported on this OS".into())
    }
}

pub fn service_stop(u: &ServiceUnit) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    {
        run_ok("systemctl", &["--user", "stop", u.unit])
    }
    #[cfg(target_os = "macos")]
    {
        run_ok("launchctl", &["bootout", &format!("gui/{}/{}", current_uid(), u.label)])
    }
    #[cfg(target_os = "windows")]
    {
        run_ok("schtasks", &["/End", "/TN", u.label])
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
    {
        let _ = u;
        Err("service control not supported on this OS".into())
    }
}

pub fn service_restart(u: &ServiceUnit) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    {
        run_ok("systemctl", &["--user", "restart", u.unit])
    }
    #[cfg(target_os = "macos")]
    {
        run_ok("launchctl", &["kickstart", "-k", &format!("gui/{}/{}", current_uid(), u.label)])
    }
    #[cfg(target_os = "windows")]
    {
        let _ = run_ok("schtasks", &["/End", "/TN", u.label]);
        run_ok("schtasks", &["/Run", "/TN", u.label])
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
    {
        let _ = u;
        Err("service control not supported on this OS".into())
    }
}

#[cfg(target_os = "macos")]
fn plist_path(u: &ServiceUnit) -> String {
    let home = std::env::var("HOME").unwrap_or_default();
    format!("{home}/Library/LaunchAgents/{}.plist", u.label)
}

/// Stop the service and remove its unit (part of uninstall). Best-effort.
fn remove_service(u: &ServiceUnit) {
    #[cfg(target_os = "linux")]
    {
        let _ = run_ok("systemctl", &["--user", "disable", "--now", u.unit]);
        if let Some(home) = std::env::var_os("HOME") {
            let _ = std::fs::remove_file(PathBuf::from(home).join(".config/systemd/user").join(u.unit));
        }
        let _ = run_ok("systemctl", &["--user", "daemon-reload"]);
    }
    #[cfg(target_os = "macos")]
    {
        let _ = run_ok("launchctl", &["bootout", &format!("gui/{}/{}", current_uid(), u.label)]);
        let _ = std::fs::remove_file(plist_path(u));
    }
    #[cfg(target_os = "windows")]
    {
        let _ = run_ok("schtasks", &["/End", "/TN", u.label]);
        let _ = run_ok("schtasks", &["/Delete", "/TN", u.label, "/F"]);
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
    {
        let _ = u;
    }
}

/// Remove the `locai` CLI symlink, but only if it points into this install root.
#[cfg(not(target_os = "windows"))]
fn remove_cli_symlink(root: &Path) {
    let mut dirs = vec![PathBuf::from("/usr/local/bin")];
    if let Some(home) = std::env::var_os("HOME") {
        dirs.push(PathBuf::from(home).join(".local/bin"));
    }
    for dir in dirs {
        let link = dir.join("locai");
        if let Ok(target) = std::fs::read_link(&link) {
            if target.starts_with(root) {
                let _ = std::fs::remove_file(&link);
                eprintln!("Removed CLI symlink {}", link.display());
            }
        }
    }
}

/// Full uninstall: deregister from Control, remove the service, drop the CLI
/// entry, and delete the install root.
pub fn uninstall(root: &Path, u: &ServiceUnit) -> Result<(), String> {
    let binary = if cfg!(target_os = "windows") { "locai-link.exe" } else { "locai-link" };
    // Positive check (has the binary) rather than a path blocklist, so a stray
    // LOCAI_INSTALL_ROOT can't point this at an unrelated directory.
    if !root.join(binary).exists() {
        return Err(format!("not a Locai install (no {binary} at {})", root.display()));
    }
    // Deregister first: it needs the session api key the wipe below removes.
    deregister(root);
    remove_service(u);

    #[cfg(target_os = "windows")]
    {
        windows_remove_files(root)?;
    }
    #[cfg(not(target_os = "windows"))]
    {
        // POSIX keeps the running binary's inode alive after unlink, so it's safe
        // to delete the tree this process is executing from.
        remove_cli_symlink(root);
        std::fs::remove_dir_all(root).map_err(|e| format!("remove {}: {e}", root.display()))?;
    }
    eprintln!("Locai Link removed.");
    Ok(())
}

/// Windows can't delete a running .exe, so drop our PATH/env entries now and hand
/// the tree to a detached `cmd` that waits for this process to exit, then removes
/// it (the macOS/Linux path just unlinks in place above).
#[cfg(target_os = "windows")]
fn windows_remove_files(root: &Path) -> Result<(), String> {
    use std::os::windows::process::CommandExt;
    const DETACHED_PROCESS: u32 = 0x0000_0008;
    const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;

    let root_str = root.to_string_lossy().to_string();
    // Drop our dir from the user PATH and clear LOCAI_ARTIFACT_BASE (the installer
    // set both). PowerShell owns HKCU\Environment; best-effort.
    let esc = root_str.replace('\'', "''");
    let ps = format!(
        "$p=[Environment]::GetEnvironmentVariable('Path','User'); \
         if ($p) {{ $p=($p -split ';' | Where-Object {{ $_ -and $_ -ne '{esc}' }}) -join ';'; \
         [Environment]::SetEnvironmentVariable('Path',$p,'User') }}; \
         [Environment]::SetEnvironmentVariable('LOCAI_ARTIFACT_BASE',$null,'User')"
    );
    let _ = Command::new("powershell").args(["-NoProfile", "-Command", &ps]).output();

    let del = format!("ping 127.0.0.1 -n 3 >nul & rmdir /S /Q \"{root_str}\"");
    Command::new("cmd")
        .args(["/C", &del])
        .creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        .spawn()
        .map_err(|e| format!("spawn cleanup: {e}"))?;
    Ok(())
}

/// Dispatch a lifecycle subcommand (`start`|`stop`|`restart`|`uninstall`) against
/// this build's service unit. Called by `main` before any runtime passthrough.
pub fn run(cmd: &str) -> ExitCode {
    let unit = shape_unit();
    let result = match cmd {
        "start" => service_start(&unit),
        "stop" => service_stop(&unit),
        "restart" => service_restart(&unit),
        "uninstall" => uninstall(&install_root(), &unit),
        other => Err(format!("unknown lifecycle command: {other}")),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("{cmd} failed: {e}");
            ExitCode::FAILURE
        }
    }
}
