// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! First-launch bootstrap (Pattern B): no `current` yet, so read `boot.json`,
//! resolve a release asset, download it, verify the SHA256, extract
//! `versions/<v>/` into the install root, and write the `current` pointer.
//!
//! Failure mapping:
//!   * exit 2: reached a server but the op failed (bad SHA, missing asset,
//!     disk-full, malformed tarball).
//!   * exit 3: couldn't reach the network at all (DNS/connect/no route), so the
//!     host UI can show an offline-specific "retry once connected".
//!
//! Reimplements `swap_bundle` from `src/link/app/updater.py` in Rust so the
//! launcher stays a single self-contained binary.

use std::fs::{self, File};
use std::io::{self, BufReader, Read, Write};
use std::path::{Path, PathBuf};

use flate2::read::GzDecoder;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use tar::Archive;

use super::boot::BootConfig;
use super::status::{emit, Status};

const VERSIONS_DIR: &str = "versions";
const STAGING_DIR: &str = "staging";
const CURRENT_SYMLINK: &str = "current";
const CURRENT_POINTER_FILE: &str = "CURRENT";

const USER_AGENT: &str = concat!("locai-link-launcher/", env!("CARGO_PKG_VERSION"));
const HTTP_TIMEOUT_SECS: u64 = 60;
const DOWNLOAD_PROGRESS_EVERY_BYTES: u64 = 4 * 1024 * 1024;

/// Bootstrap exit codes, part of the host-integration contract.
pub const EXIT_BOOTSTRAP_FAILED: u8 = 2;
pub const EXIT_BOOTSTRAP_NO_INTERNET: u8 = 3;

#[derive(Debug)]
pub enum BootstrapError {
    /// Reached a server but the operation failed (404, sha mismatch, malformed tarball).
    Operation(String),
    /// Couldn't reach the network at all (DNS / connect).
    NoInternet(String),
}

impl BootstrapError {
    pub fn stage(&self) -> &'static str {
        match self {
            BootstrapError::Operation(_) => "bootstrap",
            BootstrapError::NoInternet(_) => "discover",
        }
    }
    pub fn message(&self) -> &str {
        match self {
            BootstrapError::Operation(m) | BootstrapError::NoInternet(m) => m,
        }
    }
    pub fn exit_code(&self) -> u8 {
        match self {
            BootstrapError::Operation(_) => EXIT_BOOTSTRAP_FAILED,
            BootstrapError::NoInternet(_) => EXIT_BOOTSTRAP_NO_INTERNET,
        }
    }
}

/// Resolved asset, ready to be downloaded.
#[derive(Debug, Clone)]
struct AssetTarget {
    version: String,
    asset_name: String,
    download_url: String,
    /// Release-wide checksums.txt; preferred when present.
    checksums_url: Option<String>,
    /// Per-asset .sha256 sidecar; fallback during the transition.
    sha256_url: Option<String>,
    expected_size: Option<u64>,
}

/// Subset of the GitHub Releases-API "release" object that we care about.
#[derive(Debug, Deserialize)]
struct GhRelease {
    tag_name: String,
    #[serde(default)]
    assets: Vec<GhAsset>,
}

#[derive(Debug, Deserialize)]
struct GhAsset {
    name: String,
    browser_download_url: String,
    #[serde(default)]
    size: Option<u64>,
}

/// Run the full bootstrap. Returns Ok with the installed version on
/// success; Err mapped to an exit code on failure. Emits LOCAI_STATUS
/// records throughout.
pub fn bootstrap_from_boot(install_root: &Path) -> Result<String, BootstrapError> {
    let config = super::boot::read_boot_config(install_root).map_err(BootstrapError::Operation)?;
    let asset = resolve_asset(&config)?;
    emit(&Status::BootstrapStarted {
        stage: "download",
        asset: &asset.asset_name,
        size_total: asset.expected_size,
    });
    let staging = ensure_staging(install_root).map_err(BootstrapError::Operation)?;
    let archive = staging.join(&asset.asset_name);
    download(&asset.download_url, &archive, asset.expected_size)?;
    let expected_sha = resolve_expected_sha(&asset)?;
    verify_sha256(&archive, &expected_sha).map_err(BootstrapError::Operation)?;
    emit(&Status::BootstrapVerified {
        stage: "verify",
        sha256: &expected_sha,
    });
    let target = install_root.join(VERSIONS_DIR).join(&asset.version);
    extract_tarball(&archive, &target).map_err(BootstrapError::Operation)?;
    emit(&Status::BootstrapExtracted {
        stage: "extract",
        version: &asset.version,
    });
    write_current_pointer(install_root, &asset.version).map_err(BootstrapError::Operation)?;
    let _ = fs::remove_dir_all(staging);
    emit(&Status::BootstrapReady {
        version: &asset.version,
    });
    Ok(asset.version)
}

fn resolve_asset(config: &BootConfig) -> Result<AssetTarget, BootstrapError> {
    if let Some(direct_url) = &config.asset_url {
        return resolve_direct(direct_url);
    }
    let url = format!(
        "https://api.github.com/repos/{}/releases/latest",
        config.asset_repo
    );
    let body = http_get_string(&url)?;
    let release: GhRelease = serde_json::from_str(&body)
        .map_err(|e| BootstrapError::Operation(format!("malformed releases response: {e}")))?;
    let version = release.tag_name.trim_start_matches('v').to_string();
    let basename = config.asset_basename();
    let want = format!("{basename}-v{version}.tar.gz");
    let asset = release
        .assets
        .iter()
        .find(|a| a.name == want)
        .ok_or_else(|| {
            BootstrapError::Operation(format!(
                "release {} has no asset named {want}",
                release.tag_name
            ))
        })?;
    let checksums_url = release
        .assets
        .iter()
        .find(|a| a.name.eq_ignore_ascii_case("checksums.txt"))
        .map(|a| a.browser_download_url.clone());
    let sha_name = format!("{want}.sha256");
    let sha256_url = release
        .assets
        .iter()
        .find(|a| a.name == sha_name)
        .map(|a| a.browser_download_url.clone());
    if checksums_url.is_none() && sha256_url.is_none() {
        return Err(BootstrapError::Operation(format!(
            "release {} has neither checksums.txt nor sidecar {sha_name}",
            release.tag_name
        )));
    }
    Ok(AssetTarget {
        version,
        asset_name: asset.name.clone(),
        download_url: asset.browser_download_url.clone(),
        checksums_url,
        sha256_url,
        expected_size: asset.size,
    })
}

/// Pattern B for mirrors / air-gapped: `boot.json.asset_url` points at a
/// concrete tarball. The version is read out of the filename
/// (`...-v1.0.16.tar.gz`); checksums.txt sits beside the asset, with the
/// `<url>.sha256` sidecar as the fallback.
fn resolve_direct(url: &str) -> Result<AssetTarget, BootstrapError> {
    let asset_name = url
        .rsplit('/')
        .next()
        .map(|s| s.to_string())
        .ok_or_else(|| BootstrapError::Operation(format!("asset_url has no filename: {url}")))?;
    let version = parse_version_from_asset_name(&asset_name).ok_or_else(|| {
        BootstrapError::Operation(format!(
            "could not parse -v<version>- out of asset filename: {asset_name}"
        ))
    })?;
    let checksums_url = url
        .rsplit_once('/')
        .map(|(dir, _)| format!("{dir}/checksums.txt"));
    Ok(AssetTarget {
        version,
        asset_name,
        download_url: url.to_string(),
        checksums_url,
        sha256_url: Some(format!("{url}.sha256")),
        expected_size: None,
    })
}

fn parse_version_from_asset_name(name: &str) -> Option<String> {
    // Expect `<stem>-v<version>.tar.gz` (or `.tgz`). Strip extension, then
    // split on `-v` and take the trailing chunk up to the next `-`.
    let stem = name
        .strip_suffix(".tar.gz")
        .or_else(|| name.strip_suffix(".tgz"))?;
    let idx = stem.rfind("-v")?;
    let after = &stem[idx + 2..];
    if after.is_empty() {
        return None;
    }
    Some(after.to_string())
}

fn ensure_staging(install_root: &Path) -> Result<PathBuf, String> {
    let staging = install_root.join(STAGING_DIR);
    if staging.exists() {
        fs::remove_dir_all(&staging).map_err(|e| format!("remove stale staging dir: {e}"))?;
    }
    fs::create_dir_all(&staging).map_err(|e| format!("create staging dir: {e}"))?;
    Ok(staging)
}

fn download(url: &str, dest: &Path, expected_size: Option<u64>) -> Result<(), BootstrapError> {
    let resp = http_get_reader(url)?;
    let total = expected_size.or(resp.content_length);
    let mut reader = resp.body;
    let tmp = dest.with_extension("partial");
    let mut out = File::create(&tmp)
        .map_err(|e| BootstrapError::Operation(format!("create {}: {e}", tmp.display())))?;
    let mut buf = vec![0u8; 64 * 1024];
    let mut done: u64 = 0;
    let mut next_progress_at: u64 = DOWNLOAD_PROGRESS_EVERY_BYTES;
    loop {
        let n = reader
            .read(&mut buf)
            .map_err(|e| BootstrapError::Operation(format!("download stream: {e}")))?;
        if n == 0 {
            break;
        }
        out.write_all(&buf[..n])
            .map_err(|e| BootstrapError::Operation(format!("write {}: {e}", tmp.display())))?;
        done += n as u64;
        if done >= next_progress_at {
            emit(&Status::BootstrapProgress {
                stage: "download",
                bytes_done: done,
                bytes_total: total,
            });
            next_progress_at = done + DOWNLOAD_PROGRESS_EVERY_BYTES;
        }
    }
    out.flush()
        .map_err(|e| BootstrapError::Operation(format!("flush {}: {e}", tmp.display())))?;
    drop(out);
    fs::rename(&tmp, dest).map_err(|e| {
        BootstrapError::Operation(format!(
            "rename {} -> {}: {e}",
            tmp.display(),
            dest.display()
        ))
    })?;
    emit(&Status::BootstrapProgress {
        stage: "download",
        bytes_done: done,
        bytes_total: total,
    });
    Ok(())
}

/// checksums.txt first; the .sha256 sidecar covers releases (and mirrors)
/// from before the transition. Offline errors propagate immediately so the
/// exit-code contract stays truthful.
fn resolve_expected_sha(asset: &AssetTarget) -> Result<String, BootstrapError> {
    resolve_expected_sha_with(asset, http_get_string)
}

/// The checksum-source decision, with the body fetcher injected so the branches
/// are testable without a network. checksums.txt first, then the .sha256
/// sidecar. A NoInternet fetch propagates immediately to keep the offline exit
/// code truthful; any other checksums failure falls back to the sidecar.
fn resolve_expected_sha_with(
    asset: &AssetTarget,
    fetch: impl Fn(&str) -> Result<String, BootstrapError>,
) -> Result<String, BootstrapError> {
    if let Some(url) = &asset.checksums_url {
        match fetch(url) {
            Ok(body) => match find_checksum_entry(&body, &asset.asset_name) {
                Some(sha) => return Ok(sha),
                None if asset.sha256_url.is_none() => {
                    return Err(BootstrapError::Operation(format!(
                        "no valid sha256 entry for {} in {url}",
                        asset.asset_name
                    )))
                }
                None => {} // present but no matching line; try the sidecar
            },
            Err(e @ BootstrapError::NoInternet(_)) => return Err(e),
            Err(e @ BootstrapError::Operation(_)) if asset.sha256_url.is_none() => return Err(e),
            Err(BootstrapError::Operation(_)) => {} // unreachable; try the sidecar
        }
    }
    match &asset.sha256_url {
        Some(url) => parse_sidecar(&fetch(url)?),
        None => Err(BootstrapError::Operation(
            "no checksum source resolved for the asset".into(),
        )),
    }
}

/// sha256sum format: `<hex>  <filename>` per line (`*` marks binary mode).
fn find_checksum_entry(text: &str, asset_name: &str) -> Option<String> {
    text.lines().find_map(|line| {
        let mut tokens = line.split_whitespace();
        let hex = tokens.next()?;
        let name = tokens
            .next_back()?
            .trim_start_matches('*')
            .trim_start_matches("./");
        (name == asset_name && hex.len() == 64 && hex.chars().all(|c| c.is_ascii_hexdigit()))
            .then(|| hex.to_ascii_lowercase())
    })
}

/// Sidecars are either `<sha>` or `<sha>  <filename>` (shasum/sha256sum output).
fn parse_sidecar(body: &str) -> Result<String, BootstrapError> {
    let first = body
        .split_whitespace()
        .next()
        .ok_or_else(|| BootstrapError::Operation("empty sha256 sidecar".into()))?;
    if first.len() != 64 || !first.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(BootstrapError::Operation(format!(
            "sha256 sidecar is not a 64-char hex string: {first}"
        )));
    }
    Ok(first.to_ascii_lowercase())
}

fn verify_sha256(path: &Path, expected_hex: &str) -> Result<(), String> {
    let f = File::open(path).map_err(|e| format!("open {}: {e}", path.display()))?;
    let mut reader = BufReader::new(f);
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let n = reader
            .read(&mut buf)
            .map_err(|e| format!("read for hash: {e}"))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    let got = format!("{:x}", hasher.finalize());
    if got != expected_hex.to_ascii_lowercase() {
        return Err(format!(
            "sha256 mismatch on {}: expected {expected_hex}, got {got}",
            path.file_name()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_default()
        ));
    }
    Ok(())
}

/// Extract the tarball's `versions/<v>/` subtree into `target`. Entries outside
/// it (e.g. the launcher binary at the root) are ignored; the running launcher
/// comes from the host installer and isn't auto-updated.
fn extract_tarball(archive: &Path, target: &Path) -> Result<(), String> {
    if target.exists() {
        // A previous failed bootstrap may have left a partial dir; nuke it.
        fs::remove_dir_all(target)
            .map_err(|e| format!("remove stale {}: {e}", target.display()))?;
    }
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("create {}: {e}", parent.display()))?;
    }
    let target_name = target
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .ok_or_else(|| format!("target has no file name: {}", target.display()))?;
    let tarball = File::open(archive).map_err(|e| format!("open {}: {e}", archive.display()))?;
    let mut arch = Archive::new(GzDecoder::new(BufReader::new(tarball)));
    arch.set_preserve_permissions(true);
    let mut any = false;
    for entry in arch
        .entries()
        .map_err(|e| format!("iterate tarball entries: {e}"))?
    {
        let mut entry = entry.map_err(|e| format!("tarball entry: {e}"))?;
        // Reject unsafe entry types (symlinks, hard links) before touching the
        // filesystem: a malicious release could point a symlink outside `target`
        // that a later write would follow.
        let etype = entry.header().entry_type();
        if etype.is_symlink() || etype.is_hard_link() {
            return Err(format!(
                "tarball rejected: unsafe entry type {etype:?} at {}",
                entry
                    .path()
                    .map(|p| p.display().to_string())
                    .unwrap_or_default(),
            ));
        }
        let path = entry
            .path()
            .map_err(|e| format!("entry path: {e}"))?
            .into_owned();
        let mut parts = path.components();
        let Some(first) = parts.next() else { continue };
        if first.as_os_str() != VERSIONS_DIR {
            continue;
        }
        let Some(version_dir) = parts.next() else {
            continue;
        };
        if version_dir.as_os_str() != target_name.as_str() {
            continue;
        }
        // Anchor the destination: reject any path with ParentDir, RootDir, or
        // Prefix components. Belt-and-braces against zip-slip on top of the
        // strict `versions/<v>/` prefix filter above.
        let rel_parts: Vec<std::path::Component> = parts.collect();
        for comp in &rel_parts {
            if !matches!(
                comp,
                std::path::Component::Normal(_) | std::path::Component::CurDir
            ) {
                return Err(format!(
                    "tarball rejected: unsafe path component {comp:?} in entry {}",
                    path.display(),
                ));
            }
        }
        let rel: PathBuf = rel_parts.iter().collect();
        let out = if rel.as_os_str().is_empty() {
            target.to_path_buf()
        } else {
            target.join(&rel)
        };
        if let Some(parent) = out.parent() {
            fs::create_dir_all(parent).map_err(|e| format!("mkdir {}: {e}", parent.display()))?;
        }
        entry
            .unpack(&out)
            .map_err(|e| format!("unpack {}: {e}", out.display()))?;
        any = true;
    }
    if !any {
        return Err(format!(
            "tarball has no entries under versions/{target_name}/ — wrong asset?"
        ));
    }
    Ok(())
}

/// Write the `current` pointer. Prefers a symlink, falls back to the
/// `CURRENT` text file on hosts where symlinks aren't permitted.
/// Mirrors the rollback path in main.rs so the same shape is used on
/// both bootstrap and rollback.
fn write_current_pointer(install_root: &Path, version: &str) -> Result<(), String> {
    let symlink_path = install_root.join(CURRENT_SYMLINK);
    let target = PathBuf::from(VERSIONS_DIR).join(version);
    let tmp = install_root.join(format!(".{CURRENT_SYMLINK}.bootstrap.tmp"));
    let _ = fs::remove_file(&tmp);
    if make_symlink(&target, &tmp).is_ok() {
        fs::rename(&tmp, &symlink_path).map_err(|e| format!("rename bootstrap symlink: {e}"))?;
        remove_if_regular_file(&install_root.join(CURRENT_POINTER_FILE));
        return Ok(());
    }
    let _ = fs::remove_file(&tmp);
    let pointer = install_root.join(CURRENT_POINTER_FILE);
    let tmp = install_root.join(format!("{CURRENT_POINTER_FILE}.bootstrap.tmp"));
    fs::write(&tmp, format!("{version}\n")).map_err(|e| format!("write bootstrap pointer: {e}"))?;
    fs::rename(&tmp, &pointer).map_err(|e| format!("rename bootstrap pointer: {e}"))?;
    remove_if_symlink(&symlink_path);
    Ok(())
}

/// Remove `path` iff it's a plain file (not a symlink), to clean up a stale text
/// pointer after writing a symlink. On case-insensitive filesystems the pointer
/// and symlink paths share an inode, so a naked remove_file would delete the
/// symlink just written.
fn remove_if_regular_file(path: &Path) {
    if let Ok(meta) = fs::symlink_metadata(path) {
        if !meta.file_type().is_symlink() {
            let _ = fs::remove_file(path);
        }
    }
}

/// Symmetric guard for the reverse case: remove `path` iff it's actually a
/// symlink. See [`remove_if_regular_file`] for the case-insensitive rationale.
fn remove_if_symlink(path: &Path) {
    if let Ok(meta) = fs::symlink_metadata(path) {
        if meta.file_type().is_symlink() {
            let _ = fs::remove_file(path);
        }
    }
}

#[cfg(unix)]
fn make_symlink(target: &Path, link: &Path) -> io::Result<()> {
    std::os::unix::fs::symlink(target, link)
}

#[cfg(windows)]
fn make_symlink(target: &Path, link: &Path) -> io::Result<()> {
    std::os::windows::fs::symlink_dir(target, link)
}

// --- HTTP plumbing ---------------------------------------------------

struct HttpBody {
    body: Box<dyn Read + Send + Sync + 'static>,
    content_length: Option<u64>,
}

fn http_get_string(url: &str) -> Result<String, BootstrapError> {
    let resp = http_get_reader(url)?;
    let mut s = String::new();
    let mut reader = resp.body;
    reader
        .read_to_string(&mut s)
        .map_err(|e| BootstrapError::Operation(format!("read response from {url}: {e}")))?;
    Ok(s)
}

fn http_get_reader(url: &str) -> Result<HttpBody, BootstrapError> {
    let agent = ureq::AgentBuilder::new()
        .timeout(std::time::Duration::from_secs(HTTP_TIMEOUT_SECS))
        .user_agent(USER_AGENT)
        .build();
    let resp = agent
        .get(url)
        .set("Accept", "application/octet-stream, application/json;q=0.9")
        .call()
        .map_err(classify_ureq_err)?;
    let content_length: Option<u64> = resp
        .header("Content-Length")
        .and_then(|s| s.parse::<u64>().ok());
    Ok(HttpBody {
        body: Box::new(resp.into_reader()),
        content_length,
    })
}

fn classify_ureq_err(err: ureq::Error) -> BootstrapError {
    match err {
        ureq::Error::Status(code, resp) => {
            let url = resp.get_url().to_string();
            BootstrapError::Operation(format!("HTTP {code} from {url}"))
        }
        ureq::Error::Transport(t) => {
            let kind_msg = format!("{:?}", t.kind());
            let lower = kind_msg.to_ascii_lowercase();
            let is_offline = lower.contains("dns")
                || lower.contains("connection")
                || lower.contains("io")
                || lower.contains("connectionfailed");
            let msg = format!(
                "transport error reaching {}: {} ({})",
                t.url().map(|u| u.as_str()).unwrap_or("?"),
                t,
                kind_msg
            );
            if is_offline {
                BootstrapError::NoInternet(msg)
            } else {
                BootstrapError::Operation(msg)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_version_from_well_formed_name() {
        let v = parse_version_from_asset_name("locai-link-llm-macos-arm64-v1.0.16.tar.gz");
        assert_eq!(v.as_deref(), Some("1.0.16"));
    }

    #[test]
    fn parses_version_from_tgz_short_extension() {
        let v = parse_version_from_asset_name("locai-link-llm-stt-linux-x86_64-v2.3.4.tgz");
        assert_eq!(v.as_deref(), Some("2.3.4"));
    }

    #[test]
    fn rejects_name_without_version_marker() {
        assert!(parse_version_from_asset_name("locai-link-llm.tar.gz").is_none());
    }

    #[test]
    fn rejects_name_with_no_known_extension() {
        assert!(parse_version_from_asset_name("locai-link-llm-v1.0.0.zip").is_none());
    }

    #[test]
    fn checksum_entry_lookup() {
        let sums = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef  locai-link-llm-stt-linux-x86_64-v1.2.0.tar.gz\n\
                    FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF *./other.pkg\n\
                    short  bad.tar.gz\n";
        assert_eq!(
            find_checksum_entry(sums, "locai-link-llm-stt-linux-x86_64-v1.2.0.tar.gz").as_deref(),
            Some("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
        );
        assert_eq!(
            find_checksum_entry(sums, "other.pkg").as_deref(),
            Some("ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff")
        );
        assert!(find_checksum_entry(sums, "bad.tar.gz").is_none());
        assert!(find_checksum_entry(sums, "missing.tar.gz").is_none());
    }

    // --- checksum-source resolution (fetcher injected) ---

    fn asset_with(checksums: Option<&str>, sidecar: Option<&str>) -> AssetTarget {
        AssetTarget {
            version: "1.2.0".into(),
            asset_name: "locai-link-llm-stt-linux-x86_64-v1.2.0.tar.gz".into(),
            download_url: "https://x/asset.tar.gz".into(),
            checksums_url: checksums.map(str::to_string),
            sha256_url: sidecar.map(str::to_string),
            expected_size: None,
        }
    }

    const HEX_A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const HEX_B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    #[test]
    fn resolve_prefers_checksums_and_skips_sidecar() {
        let asset = asset_with(
            Some("https://x/checksums.txt"),
            Some("https://x/asset.tar.gz.sha256"),
        );
        let sha = resolve_expected_sha_with(&asset, |url| {
            assert!(
                url.ends_with("checksums.txt"),
                "sidecar must not be fetched"
            );
            Ok(format!("{HEX_A}  {}", asset.asset_name))
        })
        .unwrap();
        assert_eq!(sha, HEX_A);
    }

    #[test]
    fn resolve_falls_back_when_asset_absent_from_checksums() {
        let asset = asset_with(
            Some("https://x/checksums.txt"),
            Some("https://x/asset.tar.gz.sha256"),
        );
        let sha = resolve_expected_sha_with(&asset, |url| {
            if url.ends_with("checksums.txt") {
                Ok(format!("{HEX_A}  some-other-asset.tar.gz"))
            } else {
                Ok(HEX_B.to_string())
            }
        })
        .unwrap();
        assert_eq!(sha, HEX_B);
    }

    #[test]
    fn resolve_falls_back_when_checksums_unreachable() {
        let asset = asset_with(
            Some("https://x/checksums.txt"),
            Some("https://x/asset.tar.gz.sha256"),
        );
        let sha = resolve_expected_sha_with(&asset, |url| {
            if url.ends_with("checksums.txt") {
                Err(BootstrapError::Operation("HTTP 404".into()))
            } else {
                Ok(HEX_B.to_string())
            }
        })
        .unwrap();
        assert_eq!(sha, HEX_B);
    }

    #[test]
    fn resolve_propagates_offline_without_falling_back() {
        let asset = asset_with(
            Some("https://x/checksums.txt"),
            Some("https://x/asset.tar.gz.sha256"),
        );
        let err = resolve_expected_sha_with(&asset, |url| {
            if url.ends_with("checksums.txt") {
                Err(BootstrapError::NoInternet("dns".into()))
            } else {
                panic!("must not reach the sidecar when offline");
            }
        })
        .unwrap_err();
        assert!(matches!(err, BootstrapError::NoInternet(_)));
    }

    #[test]
    fn resolve_sidecar_only() {
        let asset = asset_with(None, Some("https://x/asset.tar.gz.sha256"));
        let sha = resolve_expected_sha_with(&asset, |_| Ok(HEX_B.to_string())).unwrap();
        assert_eq!(sha, HEX_B);
    }

    #[test]
    fn resolve_no_source_fails() {
        let asset = asset_with(None, None);
        let err = resolve_expected_sha_with(&asset, |_| Ok(HEX_A.to_string())).unwrap_err();
        assert!(matches!(err, BootstrapError::Operation(_)));
    }

    #[test]
    fn resolve_checksums_missing_entry_no_sidecar_fails() {
        let asset = asset_with(Some("https://x/checksums.txt"), None);
        let err = resolve_expected_sha_with(&asset, |_| Ok(format!("{HEX_A}  other.tar.gz")))
            .unwrap_err();
        assert!(matches!(err, BootstrapError::Operation(_)));
    }

    #[test]
    fn resolve_direct_derives_both_checksum_sources() {
        let t =
            resolve_direct("https://mirror.example/rel/locai-link-llm-linux-x86_64-v1.2.0.tar.gz")
                .unwrap();
        assert_eq!(
            t.checksums_url.as_deref(),
            Some("https://mirror.example/rel/checksums.txt")
        );
        assert_eq!(
            t.sha256_url.as_deref(),
            Some("https://mirror.example/rel/locai-link-llm-linux-x86_64-v1.2.0.tar.gz.sha256")
        );
    }

    #[test]
    fn verify_sha256_happy_path() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("payload");
        fs::write(&p, b"hello world").unwrap();
        // Pre-computed sha256("hello world")
        let expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9";
        verify_sha256(&p, expected).unwrap();
    }

    #[test]
    fn verify_sha256_mismatch_is_explicit() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("payload");
        fs::write(&p, b"hello world").unwrap();
        let err = verify_sha256(&p, &"0".repeat(64)).unwrap_err();
        assert!(err.contains("sha256 mismatch"), "got: {err}");
    }

    #[test]
    fn write_current_pointer_creates_symlink_on_unix() {
        #[cfg(unix)]
        {
            let dir = tempfile::tempdir().unwrap();
            fs::create_dir_all(dir.path().join("versions/1.0.16")).unwrap();
            write_current_pointer(dir.path(), "1.0.16").unwrap();
            let cur = dir.path().join("current");
            let target = fs::read_link(&cur).unwrap();
            assert_eq!(target.file_name().unwrap(), "1.0.16");
            // On case-insensitive filesystems (macOS APFS/HFS+ default),
            // `CURRENT` and `current` share the same inode — so the path
            // exists via the symlink. Assert only that no *stale text
            // pointer file* is present: if the name resolves at all, it
            // must be the symlink we just wrote.
            if let Ok(meta) = fs::symlink_metadata(dir.path().join("CURRENT")) {
                assert!(
                    meta.file_type().is_symlink(),
                    "stale CURRENT text pointer left behind next to the symlink",
                );
            }
        }
    }

    #[test]
    fn extract_tarball_pulls_only_versions_subtree() {
        // Build a synthetic tarball matching the bundling/build.py shape:
        //   locai-link             <-- launcher at root, should be SKIPPED
        //   versions/1.0.16/runtime
        //   versions/1.0.16/manifest.json
        let dir = tempfile::tempdir().unwrap();
        let archive_path = dir.path().join("payload.tar.gz");

        {
            let f = File::create(&archive_path).unwrap();
            let gz = flate2::write::GzEncoder::new(f, flate2::Compression::default());
            let mut tar = tar::Builder::new(gz);

            let mut launcher_hdr = tar::Header::new_gnu();
            launcher_hdr.set_size(7);
            launcher_hdr.set_mode(0o644);
            launcher_hdr.set_cksum();
            tar.append_data(&mut launcher_hdr, "locai-link", &b"launchr"[..])
                .unwrap();

            let mut runtime_hdr = tar::Header::new_gnu();
            runtime_hdr.set_size(8);
            runtime_hdr.set_mode(0o755);
            runtime_hdr.set_cksum();
            tar.append_data(
                &mut runtime_hdr,
                "versions/1.0.16/runtime",
                &b"RUNTIME!"[..],
            )
            .unwrap();

            let mut mf_hdr = tar::Header::new_gnu();
            mf_hdr.set_size(2);
            mf_hdr.set_mode(0o644);
            mf_hdr.set_cksum();
            tar.append_data(&mut mf_hdr, "versions/1.0.16/manifest.json", &b"{}"[..])
                .unwrap();

            tar.finish().unwrap();
        }

        let target = dir.path().join("versions/1.0.16");
        extract_tarball(&archive_path, &target).unwrap();
        assert!(target.join("runtime").is_file());
        assert!(target.join("manifest.json").is_file());
        assert!(!dir.path().join("locai-link").exists());
    }
}
