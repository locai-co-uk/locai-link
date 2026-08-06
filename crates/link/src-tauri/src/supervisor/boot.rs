// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! `boot.json` parsing: describes what to fetch on first launch when no
//! `current` is installed yet (Pattern B). The host installer drops this file
//! at `<install_root>/boot.json` alongside the launcher binary; its contents
//! are all the bootstrap path needs to resolve a download URL.

use std::fs;
use std::path::Path;

// One BootConfig schema for the whole crate; the bootstrap `asset_basename`
// behaviour is an inherent impl on the shared type below. Re-exported so the
// existing `boot::BootConfig` path (used by bootstrap.rs) keeps working.
pub use crate::shared::BootConfig;

const BOOT_JSON: &str = "boot.json";

impl BootConfig {
    /// Derive the tarball asset-name stem from `shape`, matching
    /// `bundling/manifest.py` (`asset_stem` + `platform_tag`):
    /// `locai-link-<shape>-<os>-<arch>` (e.g. `locai-link-headless-linux-x64`).
    /// Version + extension appended later.
    pub fn asset_basename(&self) -> String {
        format!(
            "locai-link-{}-{}-{}",
            self.shape,
            target_os(),
            target_arch()
        )
    }
}

pub fn read_boot_config(install_root: &Path) -> Result<BootConfig, String> {
    let path = install_root.join(BOOT_JSON);
    let body = fs::read_to_string(&path).map_err(|e| format!("read {}: {}", path.display(), e))?;
    serde_json::from_str(&body).map_err(|e| format!("parse {}: {}", path.display(), e))
}

fn target_os() -> &'static str {
    // Mirrors `_platform_tag` in `bundling/prefetch.py`: Darwin -> macos.
    if cfg!(target_os = "macos") {
        "macos"
    } else if cfg!(target_os = "linux") {
        "linux"
    } else if cfg!(target_os = "windows") {
        "windows"
    } else {
        "unknown"
    }
}

fn target_arch() -> &'static str {
    if cfg!(target_arch = "aarch64") {
        "arm64"
    } else if cfg!(target_arch = "x86_64") {
        "x64"
    } else {
        "unknown"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn tmpdir() -> tempfile::TempDir {
        tempfile::tempdir().expect("tempdir")
    }

    fn write_boot(root: &Path, body: &str) {
        let mut f = fs::File::create(root.join(BOOT_JSON)).unwrap();
        f.write_all(body.as_bytes()).unwrap();
    }

    #[test]
    fn parses_minimal_boot_json() {
        let d = tmpdir();
        write_boot(
            d.path(),
            r#"{"host_app":"Meetily","asset_repo":"locai-co-uk/locai-link"}"#,
        );
        let b = read_boot_config(d.path()).unwrap();
        assert_eq!(b.host_app, "Meetily");
        assert_eq!(b.asset_repo, "locai-co-uk/locai-link");
        assert_eq!(b.channel, "stable");
        assert!(b.plugin_set.is_empty());
        assert!(b.asset_url.is_none());
    }

    #[test]
    fn parses_full_boot_json() {
        let d = tmpdir();
        write_boot(
            d.path(),
            r#"{
                "host_app": "SafeChat",
                "plugin_set": ["llm", "stt"],
                "channel": "beta",
                "asset_repo": "locai-co-uk/locai-link",
                "asset_url": "https://example.test/release.tar.gz"
            }"#,
        );
        let b = read_boot_config(d.path()).unwrap();
        assert_eq!(b.plugin_set, vec!["llm", "stt"]);
        assert_eq!(b.channel, "beta");
        assert_eq!(
            b.asset_url.as_deref(),
            Some("https://example.test/release.tar.gz")
        );
    }

    #[test]
    fn missing_file_is_explicit_error() {
        let d = tmpdir();
        let err = read_boot_config(d.path()).unwrap_err();
        assert!(err.contains("boot.json"), "got: {err}");
    }

    #[test]
    fn malformed_json_is_explicit_error() {
        let d = tmpdir();
        write_boot(d.path(), "not json");
        let err = read_boot_config(d.path()).unwrap_err();
        assert!(err.contains("parse"), "got: {err}");
    }

    #[test]
    fn asset_basename_uses_shape() {
        let b: BootConfig =
            serde_json::from_str(r#"{"host_app":"X","shape":"headless","asset_repo":"r"}"#)
                .unwrap();
        assert!(
            b.asset_basename().starts_with("locai-link-headless-"),
            "got: {}",
            b.asset_basename()
        );
    }

    #[test]
    fn asset_basename_defaults_shape_to_desktop() {
        // boot.json without a shape (older installers) defaults to desktop.
        let b: BootConfig = serde_json::from_str(r#"{"host_app":"X","asset_repo":"r"}"#).unwrap();
        assert!(
            b.asset_basename().starts_with("locai-link-desktop-"),
            "got: {}",
            b.asset_basename()
        );
    }
}
