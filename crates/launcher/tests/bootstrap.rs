// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Integration tests for the launcher's Pattern-B first-launch bootstrap.
//!
//! Each test stands up a tempdir with `boot.json` + the launcher binary,
//! starts a local mockito server that serves a fake release tarball, then
//! spawns the launcher. The "runtime" inside the tarball is a tiny shell
//! script — the launcher exec's it after bootstrap; checking the script's
//! side effects (exit code, file touched) proves the full path ran.
//!
//! Unix-only: the runtime stub is a shell script.

#![cfg(unix)]

use std::fs;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::Duration;

use sha2::{Digest, Sha256};

const LAUNCHER: &str = env!("CARGO_BIN_EXE_locai-link");

fn tempdir() -> tempfile::TempDir {
    tempfile::tempdir().expect("create tempdir")
}

fn install_launcher(root: &Path) -> PathBuf {
    let target = root.join("locai-link");
    fs::copy(LAUNCHER, &target).unwrap();
    let mut perm = fs::metadata(&target).unwrap().permissions();
    perm.set_mode(0o755);
    fs::set_permissions(&target, perm).unwrap();
    target
}

fn run_launcher(cmd: &mut Command) -> Output {
    // Same ETXTBSY-retry shape as tests/dispatch.rs — see the long
    // comment there for why this is needed under multi-threaded cargo.
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

/// Build a tarball with a single executable `versions/<v>/locai-link-runtime`
/// shell-script entry whose body is `script_body`. Returns the tarball
/// bytes (caller decides where they live) and its lowercase-hex sha256.
fn build_release_tarball(version: &str, script_body: &str) -> (Vec<u8>, String) {
    let mut buf: Vec<u8> = Vec::new();
    {
        let gz = flate2::write::GzEncoder::new(&mut buf, flate2::Compression::default());
        let mut tar = tar::Builder::new(gz);

        let script = format!("#!/bin/sh\n{script_body}\n");
        let mut hdr = tar::Header::new_gnu();
        hdr.set_size(script.len() as u64);
        hdr.set_mode(0o755);
        hdr.set_cksum();
        tar.append_data(
            &mut hdr,
            format!("versions/{version}/locai-link-runtime"),
            script.as_bytes(),
        )
        .unwrap();

        // Drop a launcher binary at the root too — Phase 2 extract MUST
        // skip this (see bootstrap.rs::extract_tarball). If extract
        // overwrites our running launcher, the test still passes but the
        // file size would change; we verify untouched-ness below.
        let mut hdr = tar::Header::new_gnu();
        let stub = b"NOT_A_REAL_LAUNCHER";
        hdr.set_size(stub.len() as u64);
        hdr.set_mode(0o644);
        hdr.set_cksum();
        tar.append_data(&mut hdr, "locai-link", &stub[..]).unwrap();

        tar.finish().unwrap();
    }
    let mut hasher = Sha256::new();
    hasher.update(&buf);
    let sha = format!("{:x}", hasher.finalize());
    (buf, sha)
}

fn write_boot_json(root: &Path, asset_url: &str) {
    let body = format!(
        r#"{{
  "host_app": "TestHost",
  "plugin_set": [],
  "channel": "stable",
  "asset_repo": "unused/in-direct-mode",
  "asset_url": "{asset_url}"
}}"#
    );
    let mut f = fs::File::create(root.join("boot.json")).unwrap();
    f.write_all(body.as_bytes()).unwrap();
}

#[test]
fn bootstrap_happy_path_extracts_then_execs_runtime() {
    let tmp = tempdir();
    let original_launcher_size = fs::metadata(LAUNCHER).unwrap().len();
    install_launcher(tmp.path());
    let marker = tmp.path().join("ran.flag");
    let (tarball, sha) =
        build_release_tarball("1.0.16", &format!("touch {} && exit 0", marker.display()));

    let mut server = mockito::Server::new();
    let asset_path = "/locai-link-base-test-arch-v1.0.16.tar.gz";
    let _m1 = server
        .mock("GET", asset_path)
        .with_status(200)
        .with_header("content-type", "application/gzip")
        .with_body(tarball)
        .create();
    let _m2 = server
        .mock("GET", format!("{asset_path}.sha256").as_str())
        .with_status(200)
        .with_body(format!("{sha}  {}\n", asset_path.trim_start_matches('/')))
        .create();
    let asset_url = format!("{}{}", server.url(), asset_path);
    write_boot_json(tmp.path(), &asset_url);

    let out = run_launcher(&mut Command::new(tmp.path().join("locai-link")));
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success(),
        "launcher should exit 0 after runtime ran; got {:?}\nstderr:\n{stderr}",
        out.status.code()
    );
    assert!(
        marker.is_file(),
        "the extracted runtime stub should have touched its marker file"
    );

    // Pointer was written and points at 1.0.16.
    let cur = tmp.path().join("current");
    assert!(cur.exists(), "current should exist after bootstrap");
    if cur.is_symlink() {
        let target = fs::read_link(&cur).unwrap();
        assert_eq!(target.file_name().unwrap(), "1.0.16");
    } else {
        let txt = fs::read_to_string(tmp.path().join("CURRENT")).unwrap();
        assert_eq!(txt.trim(), "1.0.16");
    }

    // Bootstrap emits the documented status records to stderr.
    assert!(
        stderr.contains("LOCAI_STATUS:") && stderr.contains("\"event\":\"bootstrap_ready\""),
        "stderr should include bootstrap_ready status; got:\n{stderr}"
    );

    // The launcher binary at the install root MUST be untouched — extract
    // ignores the tarball's top-level `locai-link` entry by design.
    let post_size = fs::metadata(tmp.path().join("locai-link")).unwrap().len();
    assert_eq!(
        post_size, original_launcher_size,
        "extract must not overwrite the running launcher"
    );

    // Staging dir is cleaned up.
    assert!(
        !tmp.path().join("staging").exists(),
        "staging dir should be removed"
    );
}

#[test]
fn bootstrap_sha_mismatch_exits_2_and_emits_failed() {
    let tmp = tempdir();
    install_launcher(tmp.path());
    let (tarball, _real_sha) = build_release_tarball("1.0.16", "exit 0");
    let bogus_sha = "0".repeat(64);

    let mut server = mockito::Server::new();
    let asset_path = "/asset-v1.0.16.tar.gz";
    let _m1 = server
        .mock("GET", asset_path)
        .with_status(200)
        .with_body(tarball)
        .create();
    let _m2 = server
        .mock("GET", format!("{asset_path}.sha256").as_str())
        .with_status(200)
        .with_body(bogus_sha)
        .create();
    let asset_url = format!("{}{}", server.url(), asset_path);
    write_boot_json(tmp.path(), &asset_url);

    let out = run_launcher(&mut Command::new(tmp.path().join("locai-link")));
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert_eq!(
        out.status.code(),
        Some(2),
        "sha mismatch should exit 2; stderr:\n{stderr}"
    );
    assert!(
        stderr.contains("\"event\":\"bootstrap_failed\""),
        "stderr should include bootstrap_failed; got:\n{stderr}"
    );
    assert!(
        stderr.contains("sha256"),
        "failure message should mention sha256; got:\n{stderr}"
    );
    assert!(
        !tmp.path().join("current").exists() && !tmp.path().join("CURRENT").exists(),
        "current must NOT be written when bootstrap failed"
    );
}

#[test]
fn bootstrap_missing_asset_exits_2() {
    let tmp = tempdir();
    install_launcher(tmp.path());

    let mut server = mockito::Server::new();
    let asset_path = "/asset-v1.0.16.tar.gz";
    // Asset endpoint returns 404; no sha mock at all.
    let _m = server.mock("GET", asset_path).with_status(404).create();
    let asset_url = format!("{}{}", server.url(), asset_path);
    write_boot_json(tmp.path(), &asset_url);

    let out = run_launcher(&mut Command::new(tmp.path().join("locai-link")));
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert_eq!(
        out.status.code(),
        Some(2),
        "404 asset should exit 2; stderr:\n{stderr}"
    );
    assert!(stderr.contains("\"event\":\"bootstrap_failed\""));
}

#[test]
fn bootstrap_no_internet_exits_3() {
    let tmp = tempdir();
    install_launcher(tmp.path());
    // Port 1 is reserved/unused on every host; connect should refuse.
    write_boot_json(tmp.path(), "http://127.0.0.1:1/asset-v1.0.16.tar.gz");

    let out = run_launcher(&mut Command::new(tmp.path().join("locai-link")));
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert_eq!(
        out.status.code(),
        Some(3),
        "connect-refused should exit 3; stderr:\n{stderr}"
    );
    assert!(stderr.contains("\"event\":\"bootstrap_failed\""));
}

#[test]
fn missing_boot_json_and_no_current_is_a_clear_error() {
    let tmp = tempdir();
    install_launcher(tmp.path());
    // No boot.json, no versions/. Launcher should exit with 2 and a
    // diagnostic — Pattern-B is genuinely unconfigured.
    let out = run_launcher(&mut Command::new(tmp.path().join("locai-link")));
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert_eq!(
        out.status.code(),
        Some(2),
        "missing boot.json + missing current should exit 2; stderr:\n{stderr}"
    );
    assert!(
        stderr.contains("boot.json"),
        "error should mention boot.json; got:\n{stderr}"
    );
}
