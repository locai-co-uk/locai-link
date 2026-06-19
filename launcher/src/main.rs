// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Stable launcher for the locai-link runtime.
//!
//! Lives at `<install_root>/locai-link`. On start, resolves the `current`
//! pointer at `<install_root>/current` (a symlink to `versions/<v>/`) — or
//! the `CURRENT` text file fallback on hosts where symlinks aren't
//! permitted — and spawns `<install_root>/versions/<v>/locai-link-runtime`
//! with the original argv. Stays alive while the runtime runs; on exit
//! code 42 (the runtime's "restart for update" signal) re-resolves
//! `current` and respawns. Any other exit code is propagated.
//!
//! Phase 2 scope: exec-dispatch + restart loop only. Bootstrap branch
//! (Pattern B fetch-on-first-use) and rollback-on-early-crash come in
//! Phases 3 and 4. See ../../OTA-BUNDLE.md.

use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

const RUNTIME_BINARY_NAME: &str = if cfg!(windows) {
    "locai-link-runtime.exe"
} else {
    "locai-link-runtime"
};
const CURRENT_SYMLINK: &str = "current";
const CURRENT_POINTER_FILE: &str = "CURRENT";
const VERSIONS_DIR: &str = "versions";

/// The runtime emits this exit code when it wants to be restarted after an
/// OTA swap. See OTA-BUNDLE.md §5 step 9.
const EXIT_RESTART_FOR_UPDATE: i32 = 42;

fn main() -> ExitCode {
    match run() {
        Ok(code) => ExitCode::from(code),
        Err(e) => {
            eprintln!("locai-link launcher: {e}");
            ExitCode::from(2)
        }
    }
}

fn run() -> Result<u8, String> {
    let install_root = find_install_root()?;
    let args: Vec<std::ffi::OsString> = env::args_os().skip(1).collect();

    loop {
        let version = resolve_current_version(&install_root).ok_or_else(|| {
            format!(
                "no installed version found. \n\
                 Expected a `{CURRENT_SYMLINK}` symlink or `{CURRENT_POINTER_FILE}` pointer file in {}.\n\
                 The host installer is responsible for seeding at least one version (Pattern A) \
                 until the bootstrap branch lands (Phase 3).",
                install_root.display()
            )
        })?;

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
            Some(code) => return Ok(code.clamp(0, 255) as u8),
            // Killed by a signal (Unix) — surface as a non-zero exit so
            // supervisors notice. Don't loop: a signal-kill is not the
            // same as a needs-restart signal.
            None => return Ok(1),
        }
    }
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
