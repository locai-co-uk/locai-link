// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! On-disk install state: `boot.json` config + `current` version pointer.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// Schema of `boot.json`. `host_app`/`channel` are schema-only (written by
/// installers for later telemetry/rollout routing); the supervisor's
/// `asset_basename` (supervisor/boot.rs) reads `plugin_set`/`asset_repo`/`asset_url`.
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
    Some(InstalledVersion {
        version,
        path: resolved,
    })
}

/// Map a bundled plugin name to the Control library `model_type` it can serve.
/// Extend as new servable plugins are added; unknown plugins map to nothing.
fn plugin_model_type(plugin: &str) -> Option<&'static str> {
    match plugin {
        "language_model" => Some("language_models"),
        "audio_transcriber" => Some("audio_transcription"),
        _ => None,
    }
}

/// Model types this install can serve, derived from the current bundle's
/// plugins (`current/manifest.json`). Source of truth for filtering model lists.
pub fn supported_model_types(install_root: &Path) -> Vec<String> {
    // Unreadable manifest (source checkout / missing) → LLM fallback. A readable
    // manifest with no servable plugins → empty, so nothing unsupported is offered.
    match read_manifest_model_types(install_root) {
        Some(types) => types,
        None => vec!["language_models".to_string()],
    }
}

fn read_manifest_model_types(install_root: &Path) -> Option<Vec<String>> {
    let version = installed_version(install_root)?;
    let content = std::fs::read_to_string(version.path.join("manifest.json")).ok()?;
    let json: serde_json::Value = serde_json::from_str(&content).ok()?;
    let plugins = json.get("plugins")?.as_array()?;
    let mut out: Vec<String> = Vec::new();
    for p in plugins {
        let Some(name) = p.get("name").and_then(|n| n.as_str()) else {
            continue;
        };
        if let Some(model_type) = plugin_model_type(name) {
            let model_type = model_type.to_string();
            if !out.contains(&model_type) {
                out.push(model_type);
            }
        }
    }
    Some(out)
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
        assert_eq!(
            cfg.asset_url.as_deref(),
            Some("https://example.com/link.tar.gz")
        );
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

    // --- supported_model_types -------------------------------------------

    fn lay_down_manifest(root: &Path, plugins_json: &str) {
        let versions = root.join("versions").join("1.0.0");
        fs::create_dir_all(&versions).unwrap();
        fs::write(root.join("current"), "versions/1.0.0\n").unwrap();
        fs::write(
            versions.join("manifest.json"),
            format!(r#"{{"version":"1.0.0","plugins":{plugins_json}}}"#),
        )
        .unwrap();
    }

    #[test]
    fn supported_types_llm_and_stt_build() {
        let dir = tempdir().unwrap();
        lay_down_manifest(
            dir.path(),
            r#"[{"name":"language_model"},{"name":"audio_transcriber"}]"#,
        );
        assert_eq!(
            supported_model_types(dir.path()),
            vec!["language_models", "audio_transcription"]
        );
    }

    #[test]
    fn supported_types_llm_only_build() {
        let dir = tempdir().unwrap();
        lay_down_manifest(dir.path(), r#"[{"name":"language_model"}]"#);
        assert_eq!(supported_model_types(dir.path()), vec!["language_models"]);
    }

    #[test]
    fn supported_types_unknown_plugin_returns_empty() {
        // Readable manifest, but no plugin maps to a servable type: offer nothing
        // rather than wrongly advertising LLMs.
        let dir = tempdir().unwrap();
        lay_down_manifest(dir.path(), r#"[{"name":"image_classifier"}]"#);
        assert!(supported_model_types(dir.path()).is_empty());
    }

    #[test]
    fn supported_types_no_manifest_falls_back_to_llm() {
        let dir = tempdir().unwrap();
        assert_eq!(supported_model_types(dir.path()), vec!["language_models"]);
    }
}
