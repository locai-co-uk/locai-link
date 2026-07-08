// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! On-disk install state: `boot.json` config + `current` version pointer.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// Schema of `boot.json`. Mirrors `launcher/src/boot.rs::BootConfig` —
/// field optionality must match the launcher exactly.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BootConfig {
    pub host_app: String,
    #[serde(default)]
    pub plugin_set: Vec<String>,
    #[serde(default = "default_channel")]
    pub channel: String,
    pub asset_repo: String,
    #[serde(default)]
    pub asset_url: Option<String>,
}

fn default_channel() -> String {
    "stable".to_string()
}

/// Currently-installed version of Link, as the launcher would see it.
#[derive(Debug, Clone)]
pub struct InstalledVersion {
    pub version: String,
    pub path: PathBuf,
}

/// Parse `boot.json`. `NotFound` when missing, `InvalidData` when malformed.
pub fn read_boot_json(path: &Path) -> Result<BootConfig, std::io::Error> {
    let content = std::fs::read_to_string(path)?;
    serde_json::from_str(&content)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))
}

/// Resolve `<install_root>/current` to the version it points at.
/// Accepts either a symlink or a text-pointer file (launcher writes whichever the OS supports).
pub fn installed_version(install_root: &Path) -> Option<InstalledVersion> {
    let current = install_root.join("current");
    let raw_target = resolve_current(&current)?;
    let resolved = if raw_target.is_absolute() {
        raw_target
    } else {
        install_root.join(raw_target)
    };
    if !resolved.exists() {
        return None;
    }
    let version = resolved.file_name()?.to_string_lossy().into_owned();
    Some(InstalledVersion { version, path: resolved })
}

/// Read `current` as a symlink first, falling back to text-pointer contents.
fn resolve_current(current: &Path) -> Option<PathBuf> {
    if let Ok(target) = std::fs::read_link(current) {
        return Some(target);
    }
    let text = std::fs::read_to_string(current).ok()?;
    let trimmed = text.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(PathBuf::from(trimmed))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::fs;
    use tempfile::tempdir;

    // --- read_boot_json --------------------------------------------------

    #[test]
    fn read_boot_json_parses_a_complete_record() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("boot.json");
        fs::write(
            &path,
            r#"{
                "host_app": "locai-link",
                "plugin_set": ["language_model", "audio_transcriber"],
                "channel": "stable",
                "asset_repo": "locai-co-uk/locai-link",
                "asset_url": "https://example.com/link.tar.gz"
            }"#,
        )
        .unwrap();
        let cfg = read_boot_json(&path).unwrap();
        assert_eq!(cfg.host_app, "locai-link");
        assert_eq!(cfg.plugin_set, vec!["language_model", "audio_transcriber"]);
        assert_eq!(cfg.channel, "stable");
        assert_eq!(cfg.asset_repo, "locai-co-uk/locai-link");
        assert_eq!(cfg.asset_url.as_deref(), Some("https://example.com/link.tar.gz"));
    }

    #[test]
    fn read_boot_json_applies_defaults_for_optional_fields() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("boot.json");
        fs::write(
            &path,
            r#"{"host_app": "locai-link", "asset_repo": "locai-co-uk/locai-link"}"#,
        )
        .unwrap();
        let cfg = read_boot_json(&path).unwrap();
        assert!(cfg.plugin_set.is_empty(), "plugin_set defaults to []");
        assert_eq!(cfg.channel, "stable", "channel defaults to stable");
        assert!(cfg.asset_url.is_none());
    }

    #[test]
    fn read_boot_json_missing_file_returns_notfound() {
        let dir = tempdir().unwrap();
        let err = read_boot_json(&dir.path().join("nope.json")).unwrap_err();
        assert_eq!(err.kind(), std::io::ErrorKind::NotFound);
    }

    #[test]
    fn read_boot_json_malformed_returns_invaliddata() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("boot.json");
        fs::write(&path, "{ this is not json").unwrap();
        let err = read_boot_json(&path).unwrap_err();
        assert_eq!(err.kind(), std::io::ErrorKind::InvalidData);
    }

    // --- installed_version -----------------------------------------------

    #[test]
    #[cfg(unix)]
    fn installed_version_via_symlink() {
        let dir = tempdir().unwrap();
        let versions = dir.path().join("versions").join("1.0.17");
        fs::create_dir_all(&versions).unwrap();
        std::os::unix::fs::symlink(&versions, dir.path().join("current")).unwrap();

        let v = installed_version(dir.path()).unwrap();
        assert_eq!(v.version, "1.0.17");
        assert!(v.path.ends_with("versions/1.0.17"));
    }

    #[test]
    fn installed_version_via_text_pointer_relative() {
        let dir = tempdir().unwrap();
        let versions = dir.path().join("versions").join("1.0.17");
        fs::create_dir_all(&versions).unwrap();
        // Relative pointer resolved against install_root.
        fs::write(dir.path().join("current"), "versions/1.0.17\n").unwrap();

        let v = installed_version(dir.path()).unwrap();
        assert_eq!(v.version, "1.0.17");
        assert!(v.path.ends_with("versions/1.0.17"));
    }

    #[test]
    fn installed_version_via_text_pointer_absolute() {
        let dir = tempdir().unwrap();
        let versions = dir.path().join("versions").join("1.0.17");
        fs::create_dir_all(&versions).unwrap();
        let abs = versions.to_string_lossy().into_owned();
        fs::write(dir.path().join("current"), &abs).unwrap();

        let v = installed_version(dir.path()).unwrap();
        assert_eq!(v.version, "1.0.17");
    }

    #[test]
    fn installed_version_none_when_current_missing() {
        let dir = tempdir().unwrap();
        assert!(installed_version(dir.path()).is_none());
    }

    #[test]
    fn installed_version_none_when_target_missing() {
        let dir = tempdir().unwrap();
        fs::write(dir.path().join("current"), "versions/1.0.17").unwrap();
        // Target directory intentionally not created.
        assert!(installed_version(dir.path()).is_none());
    }

    #[test]
    fn installed_version_none_when_pointer_is_empty() {
        let dir = tempdir().unwrap();
        fs::write(dir.path().join("current"), "").unwrap();
        assert!(installed_version(dir.path()).is_none());
    }
}
