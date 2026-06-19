// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Integration tests for the launcher's exec-dispatch behavior.
//!
//! Unix-only: the stub runtimes are shell scripts. The launcher itself is
//! cross-platform; CI on Windows builds and smoke-tests via cargo build
//! instead of executing these tests.

#![cfg(unix)]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::Duration;

const LAUNCHER: &str = env!("CARGO_BIN_EXE_locai-link");

/// Copy the launcher binary into the test's install_root so `current_exe()`
/// + canonicalize resolves to the tempdir, not `target/debug/`.
fn install_launcher(root: &Path) -> PathBuf {
    let target = root.join("locai-link");
    fs::copy(LAUNCHER, &target).unwrap();
    let mut perm = fs::metadata(&target).unwrap().permissions();
    perm.set_mode(0o755);
    fs::set_permissions(&target, perm).unwrap();
    target
}

/// Run a Command, retrying on ETXTBSY ("text file busy", errno 26).
///
/// Cargo runs tests on multiple threads; each test `fs::copy`s its own
/// launcher and execs it. The multi-threaded fork-and-exec race means a
/// sibling thread's transient write fd to the copied binary can leak
/// across `fork()` into another test's child, making the subsequent
/// `execve` see the file as still-open-for-write. The window is brief —
/// a short retry loop closes it without serializing the test suite.
fn run_launcher(cmd: &mut Command) -> Output {
    for attempt in 0..10 {
        match cmd.output() {
            Ok(out) => return out,
            Err(e) if e.raw_os_error() == Some(26) => {
                std::thread::sleep(Duration::from_millis(10 * (attempt + 1)));
            }
            Err(e) => panic!("launcher should spawn: {e}"),
        }
    }
    panic!("ETXTBSY did not clear after 10 retries");
}

/// Build an install_root with `versions/<version>/locai-link-runtime` as a
/// shell script of the given body. Caller decides which pointer shape to
/// create (symlink vs CURRENT file).
fn seed_version(root: &Path, version: &str, script_body: &str) -> PathBuf {
    let version_dir = root.join("versions").join(version);
    fs::create_dir_all(&version_dir).unwrap();
    let runtime = version_dir.join("locai-link-runtime");
    fs::write(&runtime, format!("#!/bin/sh\n{script_body}\n")).unwrap();
    let mut perm = fs::metadata(&runtime).unwrap().permissions();
    perm.set_mode(0o755);
    fs::set_permissions(&runtime, perm).unwrap();
    runtime
}

fn make_current_symlink(root: &Path, version: &str) {
    let target = PathBuf::from("versions").join(version);
    std::os::unix::fs::symlink(&target, root.join("current")).unwrap();
}

fn make_current_pointer(root: &Path, version: &str) {
    fs::write(root.join("CURRENT"), format!("{version}\n")).unwrap();
}

#[test]
fn execs_runtime_via_symlink_with_argv_passthrough() {
    let tmp = tempdir();
    let launcher = install_launcher(tmp.path());
    seed_version(
        tmp.path(),
        "1.0.15",
        // Print received args, exit 0.
        r#"printf "ARGS=%s\n" "$*"; exit 0"#,
    );
    make_current_symlink(tmp.path(), "1.0.15");

    let out = run_launcher(Command::new(&launcher).args(["--device-name", "test-rig", "--api-url", "https://x/"]));

    assert!(out.status.success(), "launcher exited non-zero: {out:?}");
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(
        stdout.contains("ARGS=--device-name test-rig --api-url https://x/"),
        "argv not passed through. stdout={stdout}"
    );
}

#[test]
fn falls_back_to_current_pointer_file() {
    let tmp = tempdir();
    let launcher = install_launcher(tmp.path());
    seed_version(tmp.path(), "1.0.16", r#"echo pointer-file-runtime; exit 0"#);
    make_current_pointer(tmp.path(), "1.0.16");

    let out = run_launcher(&mut Command::new(&launcher));

    assert!(out.status.success(), "{out:?}");
    assert!(String::from_utf8_lossy(&out.stdout).contains("pointer-file-runtime"));
}

#[test]
fn restarts_on_exit_42_after_flip() {
    let tmp = tempdir();
    let launcher = install_launcher(tmp.path());
    // First version's runtime writes a marker, then flips `current` to v2 and exits 42.
    let v2_dir = tmp.path().join("versions").join("2.0.0");
    fs::create_dir_all(&v2_dir).unwrap();
    let flip_script = format!(
        r#"echo "first-run pid=$$"
ln -sfn versions/2.0.0 "{root}/current"
exit 42
"#,
        root = tmp.path().display()
    );
    seed_version(tmp.path(), "1.0.0", &flip_script);
    // v2 runtime just exits 0.
    seed_version(tmp.path(), "2.0.0", r#"echo second-run; exit 0"#);
    make_current_symlink(tmp.path(), "1.0.0");

    let out = run_launcher(&mut Command::new(&launcher));
    assert!(out.status.success(), "{out:?}");
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(stdout.contains("first-run pid="), "v1 did not run: {stdout}");
    assert!(stdout.contains("second-run"), "v2 was not re-execed: {stdout}");
}

#[test]
fn errors_when_no_current_pointer_exists() {
    let tmp = tempdir();
    let launcher = install_launcher(tmp.path());
    // No `current` symlink, no `CURRENT` file, no versions.
    let out = run_launcher(&mut Command::new(&launcher));
    assert!(!out.status.success(), "expected failure, got success: {out:?}");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("no installed version found"),
        "unexpected stderr: {stderr}"
    );
}

#[test]
fn errors_when_current_points_at_missing_version() {
    let tmp = tempdir();
    let launcher = install_launcher(tmp.path());
    make_current_pointer(tmp.path(), "999.0.0");
    // No versions/999.0.0/ exists.

    let out = run_launcher(&mut Command::new(&launcher));
    assert!(!out.status.success(), "{out:?}");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(stderr.contains("does not exist"), "unexpected stderr: {stderr}");
}

#[test]
fn propagates_runtime_nonzero_exit_code() {
    let tmp = tempdir();
    let launcher = install_launcher(tmp.path());
    seed_version(tmp.path(), "1.0.15", r#"exit 17"#);
    make_current_symlink(tmp.path(), "1.0.15");

    let out = run_launcher(&mut Command::new(&launcher));
    assert_eq!(out.status.code(), Some(17), "{out:?}");
}

// ---------------------------------------------------------------------------
// Minimal tempdir helper — avoids pulling in `tempfile` as a dev-dep for one use.
// ---------------------------------------------------------------------------

struct TempDir(PathBuf);

impl TempDir {
    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn tempdir() -> TempDir {
    // Use process PID + nanosecond counter for uniqueness across tests
    // running concurrently within the same binary.
    use std::sync::atomic::{AtomicU64, Ordering};
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("locai-link-launcher-test-{}-{n}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    TempDir(dir)
}
