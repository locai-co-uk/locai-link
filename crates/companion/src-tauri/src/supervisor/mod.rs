// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Stable launcher for the locai-link runtime.
//!
//! Lives at `<install_root>/locai-link`. Resolves the `current` pointer
//! (`current` symlink → `versions/<v>/`, or the `CURRENT` text file where
//! symlinks aren't permitted) and spawns the runtime with the original argv.
//! Exit code 42 = the runtime's "restart for update" signal: re-resolve
//! `current` (an OTA may have flipped it) and respawn.
//!
//! Rollback: a non-zero runtime exit within `POST_UPDATE_HEALTH_WINDOW_SECS`
//! of an OTA flip reads the `.update-pending` stamp, points `current` back at
//! the previous version, clears the stamp, and respawns. Past the window,
//! normal crash handling applies and the stamp is cleared.

mod boot;
mod bootstrap;
mod status;

use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const RUNTIME_BINARY_NAME: &str = if cfg!(windows) {
    "locai-link-runtime.exe"
} else {
    "locai-link-runtime"
};
const CURRENT_SYMLINK: &str = "current";
const CURRENT_POINTER_FILE: &str = "CURRENT";
const PREVIOUS_SYMLINK: &str = "previous";
const PREVIOUS_POINTER_FILE: &str = "PREVIOUS";
const VERSIONS_DIR: &str = "versions";
const UPDATE_PENDING_STAMP: &str = ".update-pending";

/// The runtime emits this exit code when it wants to be restarted after an
/// OTA swap. Same value `updater.swap_bundle` uses in `_apply_update_and_reexec`.
const EXIT_RESTART_FOR_UPDATE: i32 = 42;

/// Maximum wall-clock seconds after an OTA flip during which a non-zero
/// runtime exit triggers rollback to the previous version. Beyond this the
/// stamp is treated as stale.
const POST_UPDATE_HEALTH_WINDOW_SECS: u64 = 120;

/// Headless entry point: the supervisor loop (resolve `current`, spawn the
/// runtime child, exit-42 respawn, `.update-pending` rollback, Pattern-B
/// bootstrap). Was the standalone launcher's `main`. In `ui` builds the desktop
/// app starts this on a background thread instead (see P2).
pub fn run_supervisor() -> ExitCode {
    match run() {
        Ok(code) => ExitCode::from(code),
        Err(e) => {
            eprintln!("locai-link launcher: {e}");
            ExitCode::from(2)
        }
    }
}

/// In-process control of the runtime child for the desktop (`ui`) build. The
/// tray/preferences send Start/Stop/Restart and read status; `supervise_forever`
/// honours them. Cloneable (Arc) so it can live in Tauri managed state.
#[derive(Clone, Default)]
pub struct SupervisorControl {
    inner: Arc<Mutex<ControlState>>,
}

#[derive(Default)]
struct ControlState {
    /// What the user wants: keep the runtime running, or leave it stopped.
    want_running: bool,
    /// One-shot: kill + respawn even while `want_running`.
    restart: bool,
    /// A child is currently alive (read by the tray).
    running: bool,
}

impl SupervisorControl {
    /// Start out wanting the runtime up (normal launch).
    pub fn running() -> Self {
        let c = Self::default();
        c.inner.lock().expect("control poisoned").want_running = true;
        c
    }
    pub fn start(&self) {
        self.inner.lock().expect("control poisoned").want_running = true;
    }
    pub fn stop(&self) {
        self.inner.lock().expect("control poisoned").want_running = false;
    }
    pub fn restart(&self) {
        let mut g = self.inner.lock().expect("control poisoned");
        g.want_running = true;
        g.restart = true;
    }
    pub fn is_running(&self) -> bool {
        self.inner.lock().expect("control poisoned").running
    }
    fn want_running(&self) -> bool {
        self.inner.lock().expect("control poisoned").want_running
    }
    fn take_restart(&self) -> bool {
        let mut g = self.inner.lock().expect("control poisoned");
        std::mem::take(&mut g.restart)
    }
    fn set_running(&self, v: bool) {
        self.inner.lock().expect("control poisoned").running = v;
    }
}

enum Outcome {
    /// Killed by us (Stop or Restart).
    Interrupted,
    /// Child exited on its own; `Some(code)` or `None` (signal).
    Exited(Option<i32>),
}

fn next_backoff(b: Duration) -> Duration {
    (b * 2).min(Duration::from_secs(30))
}

/// Desktop in-process supervision: keep the runtime child alive on a background
/// thread, honouring Start/Stop/Restart from `control`. Reuses the same resolve
/// / bootstrap / rollback helpers as the headless `run()`; unlike `run()` the
/// wait is interruptible so a Stop can kill the child. Runs forever.
///
/// TODO(P3): graceful shutdown — SIGTERM + wait + SIGKILL fallback so the Python
/// runtime cleans up its engine subprocesses (llama-swap/llama-server) instead
/// of being SIGKILLed and orphaning them.
pub fn supervise_forever(control: SupervisorControl) {
    let install_root = match find_install_root() {
        Ok(r) => r,
        Err(e) => {
            eprintln!("[supervisor] {e}");
            return;
        }
    };
    let poll = Duration::from_millis(200);
    let mut backoff = Duration::from_secs(1);

    loop {
        if !control.want_running() {
            control.set_running(false);
            std::thread::sleep(poll);
            continue;
        }

        let version = match resolve_current_version(&install_root) {
            Some(v) => v,
            None => {
                if !install_root.join("boot.json").is_file() {
                    eprintln!("[supervisor] no installed version and no boot.json");
                    std::thread::sleep(backoff);
                    backoff = next_backoff(backoff);
                    continue;
                }
                match bootstrap::bootstrap_from_boot(&install_root) {
                    Ok(v) => v,
                    Err(e) => {
                        status::emit(&status::Status::BootstrapFailed {
                            stage: e.stage(),
                            error: e.message(),
                        });
                        eprintln!("[supervisor] bootstrap failed: {}", e.message());
                        std::thread::sleep(backoff);
                        backoff = next_backoff(backoff);
                        continue;
                    }
                }
            }
        };

        let runtime = install_root
            .join(VERSIONS_DIR)
            .join(&version)
            .join(RUNTIME_BINARY_NAME);
        if !runtime.is_file() {
            eprintln!("[supervisor] runtime missing: {}", runtime.display());
            std::thread::sleep(backoff);
            backoff = next_backoff(backoff);
            continue;
        }

        // The runtime is always launched in `run` mode (the GUI app isn't
        // invoked with that arg the way the headless service is).
        let mut child = match Command::new(&runtime).arg("run").spawn() {
            Ok(c) => c,
            Err(e) => {
                eprintln!("[supervisor] spawn {}: {e}", runtime.display());
                std::thread::sleep(backoff);
                backoff = next_backoff(backoff);
                continue;
            }
        };
        control.set_running(true);

        // Interruptible wait: poll for exit while honouring Stop/Restart.
        let outcome = loop {
            if !control.want_running() || control.take_restart() {
                let _ = child.kill();
                let _ = child.wait();
                break Outcome::Interrupted;
            }
            match child.try_wait() {
                Ok(Some(st)) => break Outcome::Exited(st.code()),
                Ok(None) => std::thread::sleep(poll),
                Err(e) => {
                    eprintln!("[supervisor] wait: {e}");
                    break Outcome::Exited(Some(1));
                }
            }
        };
        control.set_running(false);

        match outcome {
            // Stop -> idle next iteration; Restart -> respawn immediately.
            Outcome::Interrupted => backoff = Duration::from_secs(1),
            // OTA: re-resolve `current` (an update may have flipped it) + respawn.
            Outcome::Exited(Some(EXIT_RESTART_FOR_UPDATE)) => backoff = Duration::from_secs(1),
            // Clean stop: leave it stopped until the user starts it again.
            Outcome::Exited(Some(0)) => {
                control.stop();
            }
            // Crash: roll back if inside the OTA window, else respawn with backoff.
            Outcome::Exited(code) => {
                let code = code.unwrap_or(1);
                if try_rollback(&install_root, &version, code).unwrap_or(false) {
                    backoff = Duration::from_secs(1);
                } else {
                    eprintln!("[supervisor] runtime exited {code}; respawning in {backoff:?}");
                    std::thread::sleep(backoff);
                    backoff = next_backoff(backoff);
                }
            }
        }
    }
}

fn run() -> Result<u8, String> {
    let install_root = find_install_root()?;
    let args: Vec<std::ffi::OsString> = env::args_os().skip(1).collect();

    loop {
        let version = match resolve_current_version(&install_root) {
            Some(v) => v,
            None => {
                // No `current` yet — Pattern B first launch. Try to
                // bootstrap from boot.json. The host installer is
                // responsible for dropping that file alongside this
                // launcher. If it's also missing, that's a clear
                // installer bug and we surface it as exit 2.
                if !install_root.join("boot.json").is_file() {
                    return Err(format!(
                        "no installed version and no boot.json in {}. \n\
                         Either the host installer didn't drop a `boot.json` (Pattern B) \
                         nor pre-seed a `versions/<v>/` + `current` (Pattern A).",
                        install_root.display()
                    ));
                }
                match bootstrap::bootstrap_from_boot(&install_root) {
                    Ok(v) => v,
                    Err(e) => {
                        status::emit(&status::Status::BootstrapFailed {
                            stage: e.stage(),
                            error: e.message(),
                        });
                        eprintln!("locai-link launcher: bootstrap failed: {}", e.message());
                        return Ok(e.exit_code());
                    }
                }
            }
        };

        let runtime = install_root
            .join(VERSIONS_DIR)
            .join(&version)
            .join(RUNTIME_BINARY_NAME);
        if !runtime.is_file() {
            return Err(format!(
                "current points at version {version}, but {} does not exist or isn't a file.",
                runtime.display()
            ));
        }

        let status = Command::new(&runtime)
            .args(&args)
            .status()
            .map_err(|e| format!("failed to spawn {}: {e}", runtime.display()))?;

        match status.code() {
            Some(EXIT_RESTART_FOR_UPDATE) => {
                // Runtime asked to be restarted; re-resolve `current`,
                // which an OTA swap may have flipped to a new version.
                continue;
            }
            Some(0) => return Ok(0),
            Some(code) => {
                // Non-zero exit. If we're inside the post-update health
                // window for an OTA that just flipped, roll back; otherwise
                // surface the exit code.
                if try_rollback(&install_root, &version, code)? {
                    continue;
                }
                return Ok(code.clamp(0, 255) as u8);
            }
            // Killed by a signal (Unix) — surface as a non-zero exit so
            // supervisors notice. Don't loop: a signal-kill is not the
            // same as a needs-restart signal.
            None => return Ok(1),
        }
    }
}

/// Inspect the `.update-pending` stamp; if a rollback applies, perform it
/// and return `Ok(true)` so the caller respawns. Otherwise return `Ok(false)`.
///
/// Conditions for rollback: stamp present, age within
/// `POST_UPDATE_HEALTH_WINDOW_SECS`, recorded previous version still exists
/// on disk. If the stamp is older than the window we delete it (one-shot
/// cleanup) and fall through to normal exit handling.
fn try_rollback(install_root: &Path, failed_version: &str, exit_code: i32) -> Result<bool, String> {
    let pending = match read_update_pending(install_root) {
        Some(p) => p,
        None => return Ok(false),
    };

    let age_secs = now_unix().saturating_sub(pending.flipped_at_unix);
    if age_secs > POST_UPDATE_HEALTH_WINDOW_SECS {
        // Stamp is stale — outside the window. Clear it so we don't keep
        // checking on every future crash, but don't roll back.
        let _ = fs::remove_file(install_root.join(UPDATE_PENDING_STAMP));
        return Ok(false);
    }

    let previous_dir = install_root
        .join(VERSIONS_DIR)
        .join(&pending.previous_version);
    if !previous_dir.is_dir() {
        // Previous version was GC'd or never existed — nothing to roll back
        // to. Clear the stamp; surface the crash.
        let _ = fs::remove_file(install_root.join(UPDATE_PENDING_STAMP));
        return Ok(false);
    }

    eprintln!(
        "locai-link launcher: runtime {failed_version} exited {exit_code} {age_secs}s after OTA flip — \
         rolling back to {prev} and respawning.",
        prev = pending.previous_version,
    );
    write_current_pointer(install_root, &pending.previous_version)?;
    // Demote both `previous` pointer shapes — the rollback target is now
    // current, so there's nothing left to demote to.
    let _ = fs::remove_file(install_root.join(PREVIOUS_SYMLINK));
    let _ = fs::remove_file(install_root.join(PREVIOUS_POINTER_FILE));
    let _ = fs::remove_file(install_root.join(UPDATE_PENDING_STAMP));
    Ok(true)
}

struct UpdatePending {
    flipped_at_unix: u64,
    previous_version: String,
}

fn read_update_pending(install_root: &Path) -> Option<UpdatePending> {
    let content = fs::read_to_string(install_root.join(UPDATE_PENDING_STAMP)).ok()?;
    let mut lines = content.lines();
    let flipped_at_unix = lines.next()?.trim().parse::<u64>().ok()?;
    let previous_version = lines.next()?.trim().to_string();
    if previous_version.is_empty() {
        return None;
    }
    Some(UpdatePending {
        flipped_at_unix,
        previous_version,
    })
}

fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Write the `current` pointer atomically, preserving the install's shape.
/// If a `current` symlink already exists, the new pointer is also a symlink;
/// otherwise a `CURRENT` text file is written. Falls back to the pointer
/// file shape on platforms where symlink creation isn't permitted.
fn write_current_pointer(install_root: &Path, version: &str) -> Result<(), String> {
    let symlink_path = install_root.join(CURRENT_SYMLINK);
    let prefer_symlink =
        symlink_path.is_symlink() || !install_root.join(CURRENT_POINTER_FILE).is_file();

    if prefer_symlink {
        let target = PathBuf::from(VERSIONS_DIR).join(version);
        let tmp = install_root.join(format!(".{CURRENT_SYMLINK}.rollback.tmp"));
        let _ = fs::remove_file(&tmp);
        match make_symlink(&target, &tmp) {
            Ok(()) => {
                fs::rename(&tmp, &symlink_path)
                    .map_err(|e| format!("rename rollback symlink: {e}"))?;
                // If a stale pointer file is hanging around, get rid of it —
                // but only if it's a regular file. On case-insensitive
                // filesystems (macOS APFS/HFS+ default) `CURRENT` and
                // `current` share an inode; a naked remove would delete the
                // symlink we just wrote.
                remove_if_regular_file(&install_root.join(CURRENT_POINTER_FILE));
                return Ok(());
            }
            Err(_) => {
                // Symlink not permitted on this platform — fall through to
                // the pointer file shape.
                let _ = fs::remove_file(&tmp);
            }
        }
    }

    let pointer = install_root.join(CURRENT_POINTER_FILE);
    let tmp = install_root.join(format!("{CURRENT_POINTER_FILE}.rollback.tmp"));
    fs::write(&tmp, format!("{version}\n")).map_err(|e| format!("write rollback pointer: {e}"))?;
    fs::rename(&tmp, &pointer).map_err(|e| format!("rename rollback pointer: {e}"))?;
    // Clean up any stale symlink from a previous shape — same
    // case-insensitive caveat as above.
    remove_if_symlink(&symlink_path);
    Ok(())
}

/// Remove `path` iff it's a plain file (not a symlink). See the
/// case-insensitive-filesystem note on the call sites above.
fn remove_if_regular_file(path: &Path) {
    if let Ok(meta) = fs::symlink_metadata(path) {
        if !meta.file_type().is_symlink() {
            let _ = fs::remove_file(path);
        }
    }
}

/// Symmetric guard — remove `path` iff it's actually a symlink.
fn remove_if_symlink(path: &Path) {
    if let Ok(meta) = fs::symlink_metadata(path) {
        if meta.file_type().is_symlink() {
            let _ = fs::remove_file(path);
        }
    }
}

#[cfg(unix)]
fn make_symlink(target: &Path, link: &Path) -> std::io::Result<()> {
    std::os::unix::fs::symlink(target, link)
}

#[cfg(windows)]
fn make_symlink(target: &Path, link: &Path) -> std::io::Result<()> {
    std::os::windows::fs::symlink_dir(target, link)
}

/// Locate the install_root: the directory containing this launcher binary.
fn find_install_root() -> Result<PathBuf, String> {
    let exe = env::current_exe().map_err(|e| format!("could not determine launcher path: {e}"))?;
    // Resolve symlinks so a `locai-link` symlink on PATH still finds the
    // real install_root.
    let resolved = fs::canonicalize(&exe).unwrap_or(exe);
    resolved
        .parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| format!("launcher has no parent dir: {}", resolved.display()))
}

/// Read the `current` pointer. Two shapes:
///   - `current` symlink → `versions/<v>/` (POSIX, Windows w/ Developer Mode).
///   - `CURRENT` text file containing the version string (fallback).
fn resolve_current_version(install_root: &Path) -> Option<String> {
    // Prefer the symlink. read_link returns the target; we take its file
    // name (the version part of `versions/<v>`).
    let symlink = install_root.join(CURRENT_SYMLINK);
    if let Ok(target) = fs::read_link(&symlink) {
        if let Some(name) = target.file_name() {
            return Some(name.to_string_lossy().into_owned());
        }
    }
    // Fall back to the pointer file.
    let pointer = install_root.join(CURRENT_POINTER_FILE);
    fs::read_to_string(&pointer)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}
