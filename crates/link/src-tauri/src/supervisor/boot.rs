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

/// Canonical order of plugin codes in the asset name; the tarball is named in
/// this order. MUST mirror `PLUGIN_ORDER` in `bundling/manifest.py`.
const PLUGIN_ORDER: &[&str] = &["llm", "stt"];

impl BootConfig {
    /// Derive the tarball asset-name stem from `plugin_set`, matching
    /// `bundling/build.py`: `locai-link-<codes-in-canonical-order>-<os>-<arch>`
    /// (e.g. `locai-link-llm-stt-linux-x86_64`). Version + extension appended later.
    pub fn asset_basename(&self) -> String {
        let codes: Vec<&str> = PLUGIN_ORDER
            .iter()
            .copied()
            .filter(|code| self.plugin_set.iter().any(|p| p == code))
            .collect();
        let unknown: Vec<&String> = self
            .plugin_set
            .iter()
            .filter(|p| !PLUGIN_ORDER.contains(&p.as_str()))
            .collect();
        // Unknown plugins land at the end in input order, so a bad name
        // surfaces later via the release-asset lookup rather than being dropped.
        let mut joined: Vec<String> = codes.iter().map(|s| (*s).to_string()).collect();
        joined.extend(unknown.into_iter().cloned());
        let plugins = if joined.is_empty() {
            "base".to_string()
        } else {
            joined.join("-")
        };
        format!("locai-link-{}-{}-{}", plugins, target_os(), target_arch())
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
        "x86_64"
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
    fn asset_basename_uses_canonical_plugin_order() {
        let b: BootConfig =
            serde_json::from_str(r#"{"host_app":"X","plugin_set":["stt","llm"],"asset_repo":"r"}"#)
                .unwrap();
        // PLUGIN_ORDER hardcodes "llm" before "stt" to mirror
        // bundling/manifest.py — same name regardless of input order.
        assert!(
            b.asset_basename().starts_with("locai-link-llm-stt-"),
            "got: {}",
            b.asset_basename()
        );
    }

    #[test]
    fn asset_basename_unknown_plugins_appended_in_order() {
        let b: BootConfig =
            serde_json::from_str(r#"{"host_app":"X","plugin_set":["zzz","llm"],"asset_repo":"r"}"#)
                .unwrap();
        // Known plugin first, unknown trailing.
        assert!(
            b.asset_basename().starts_with("locai-link-llm-zzz-"),
            "got: {}",
            b.asset_basename()
        );
    }

    #[test]
    fn asset_basename_empty_plugins_uses_base() {
        let b: BootConfig =
            serde_json::from_str(r#"{"host_app":"X","plugin_set":[],"asset_repo":"r"}"#).unwrap();
        assert!(
            b.asset_basename().starts_with("locai-link-base-"),
            "got: {}",
            b.asset_basename()
        );
    }
}
