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
    // The Unix CLI is a symlink (<bin dir>/locai -> <root>/locai-link) and
    // macOS current_exe() returns the invoked path unresolved, so canonicalize
    // to land in the real install root. Windows launches the exe directly via
    // the .cmd shim, and canonicalize's verbatim (\\?\) form would break the
    // cleanup's path comparisons, so it keeps the raw path.
    #[cfg(not(target_os = "windows"))]
    let exe = std::env::current_exe().and_then(|p| p.canonicalize()).ok();
    #[cfg(target_os = "windows")]
    let exe = std::env::current_exe().ok();
    exe.and_then(|p| p.parent().map(Path::to_path_buf))
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
        // /End only terminates the process the scheduler launched (the hidden
        // launcher), orphaning the exe and runtime; kill this install's
        // processes explicitly.
        let _ = run_ok("schtasks", &["/End", "/TN", u.label]);
        kill_install_processes(&install_root());
        Ok(())
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
    {
        let _ = u;
        Err("service control not supported on this OS".into())
    }
}

/// Kill this install's service processes, path-scoped so other checkouts'
/// binaries (same image names) are untouched. Excludes the invoking process:
/// the CLI runs from the same install root.
#[cfg(target_os = "windows")]
fn kill_install_processes(root: &Path) {
    // Normalize best-effort: a relative or trailing-separator LOCAI_INSTALL_ROOT
    // would build a -like pattern that matches nothing and skip every process.
    let root = resolved(root).unwrap_or_else(|_| root.to_path_buf());
    let esc = root.to_string_lossy().replace('\'', "''");
    let me = std::process::id();
    let ps = format!(
        "Get-Process -Name 'locai-link','locai-link-runtime' -ErrorAction SilentlyContinue | \
         Where-Object {{ $_.Path -like '{esc}*' -and $_.Id -ne {me} }} | \
         Stop-Process -Force -ErrorAction SilentlyContinue"
    );
    let _ = Command::new("powershell").args(["-NoProfile", "-Command", &ps]).output();
}

pub fn service_restart(u: &ServiceUnit) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    {
        run_ok("systemctl", &["--user", "restart", u.unit])
    }
    #[cfg(target_os = "macos")]
    {
        // `stop` boots the job out entirely, so kickstart alone can't restart a
        // stopped service. Re-bootstrap first (errors if already loaded, which
        // is fine), then kickstart the loaded job.
        let uid = current_uid();
        let _ = run_ok("launchctl", &["bootstrap", &format!("gui/{uid}"), &plist_path(u)]);
        run_ok("launchctl", &["kickstart", "-k", &format!("gui/{uid}/{}", u.label)])
    }
    #[cfg(target_os = "windows")]
    {
        // Same orphan problem as stop: clear the old tree fully before /Run,
        // or two supervisors race for the ports.
        let _ = run_ok("schtasks", &["/End", "/TN", u.label]);
        kill_install_processes(&install_root());
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
/// `root` is uninstall's already-validated install root.
fn remove_service(u: &ServiceUnit, root: &Path) {
    #[cfg(not(target_os = "windows"))]
    let _ = root; // only the Windows process kill is path-scoped
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
        // /End is asynchronous; deleting while an instance still runs can fail
        // silently, and a surviving definition resurrects the service via
        // restart-on-failure, re-locking the install root mid-uninstall. Kill
        // the tree first, then delete, then verify the definition is gone.
        let _ = run_ok("schtasks", &["/End", "/TN", u.label]);
        kill_install_processes(root);
        let mut deleted = false;
        for _ in 0..10 {
            let _ = run_ok("schtasks", &["/Delete", "/TN", u.label, "/F"]);
            if run_ok("schtasks", &["/Query", "/TN", u.label]).is_err() {
                deleted = true;
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(500));
        }
        if !deleted {
            eprintln!("warning: scheduled task {} still registered; remove it manually", u.label);
        }
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
            // Resolve relative targets against the link's dir and canonicalize
            // (root is canonical), or a /tmp-style symlinked prefix never matches.
            let abs = if target.is_absolute() { target } else { dir.join(target) };
            let abs = abs.canonicalize().unwrap_or(abs);
            if abs.starts_with(root) {
                let _ = std::fs::remove_file(&link);
                eprintln!("Removed CLI symlink {}", link.display());
            }
        }
    }
}

/// Canonicalise, then strip the Windows verbatim (`\\?\`) prefix so paths from
/// every source compare equal and match the normal-form paths Get-Process
/// reports. The one normal form all path comparisons in this module use.
fn resolved(p: &Path) -> std::io::Result<PathBuf> {
    let p = p.canonicalize()?;
    #[cfg(target_os = "windows")]
    let p = PathBuf::from(p.to_string_lossy().trim_start_matches(r"\\?\").to_string());
    Ok(p)
}

/// Full uninstall: deregister from Control, remove the service, drop the CLI
/// entry, and delete the install root.
fn home_dir() -> Option<PathBuf> {
    let var = if cfg!(target_os = "windows") { "USERPROFILE" } else { "HOME" };
    std::env::var_os(var).map(PathBuf::from).and_then(|p| resolved(&p).ok())
}

pub fn uninstall(root: &Path, u: &ServiceUnit) -> Result<(), String> {
    let binary = if cfg!(target_os = "windows") { "locai-link.exe" } else { "locai-link" };
    // Positive layout check rather than a path blocklist: a genuine install
    // root carries the binary AND the bundle layout, so a stray or hostile
    // LOCAI_INSTALL_ROOT pointing at an unrelated directory is never deleted.
    let root = resolved(root).map_err(|e| format!("cannot resolve {}: {e}", root.display()))?;
    let root = root.as_path();
    if !(root.join(binary).is_file() && root.join("boot.json").is_file() && root.join("versions").is_dir()) {
        return Err(format!(
            "not a Locai install root (need {binary} + boot.json + versions/ at {})",
            root.display()
        ));
    }
    // parent() is None only at a filesystem root (covers Windows volume roots too).
    if root.parent().is_none() || Some(root) == home_dir().as_deref() {
        return Err(format!("refusing to delete {}", root.display()));
    }
    // Deregister first: it needs the session api key the wipe below removes.
    deregister(root);
    remove_service(u, root);

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
    #[cfg(target_os = "windows")]
    eprintln!("Locai Link removal finishing in the background (a few seconds).");
    #[cfg(not(target_os = "windows"))]
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

    // The cleanup runs as a script FILE from %TEMP% (the run-from-outside
    // pattern standard Windows uninstallers use): -File avoids the -Command
    // quoting minefield entirely, and the temp location means the script never
    // blocks the directory it deletes. A surviving service instance holds the
    // exe open and defeats a single Remove-Item, so it stops this install's
    // processes first (path-scoped: other checkouts share the image names),
    // retries the delete, and records the outcome in locai-uninstall.log.
    let script = format!(
        r#"Start-Sleep -Seconds 2
Get-Process -Name 'locai-link','locai-link-runtime' -ErrorAction SilentlyContinue |
  Where-Object {{ $_.Path -like '{esc}*' }} | Stop-Process -Force -ErrorAction SilentlyContinue
for ($i = 0; $i -lt 30; $i++) {{
  Remove-Item -LiteralPath '{esc}' -Recurse -Force -ErrorAction SilentlyContinue
  if (-not (Test-Path -LiteralPath '{esc}')) {{ break }}
  Start-Sleep -Milliseconds 500
}}
$left = @(Get-Process -Name 'locai-link','locai-link-runtime' -ErrorAction SilentlyContinue |
  Where-Object {{ $_.Path -like '{esc}*' }}).Count
"removed=$(-not (Test-Path -LiteralPath '{esc}')) leftoverProcs=$left" |
  Out-File -FilePath (Join-Path $env:TEMP 'locai-uninstall.log')
"#
    );
    let script_path = std::env::temp_dir().join("locai-uninstall.ps1");
    std::fs::write(&script_path, script).map_err(|e| format!("write cleanup script: {e}"))?;
    Command::new("powershell")
        .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File"])
        .arg(&script_path)
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
