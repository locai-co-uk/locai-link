// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Reading Link's on-disk install state — the `boot.json` config and the
//! `current` pointer under an `<install_root>`. Both surfaces (Setup
//! Assistant, menu-bar app) call these to answer "is there already an
//! install here, and what version is it?".
//!
//! Both functions still return non-panicking interim values
//! (`Err(NotFound)`, `None`). Real implementations land with the Setup
//! Assistant's existing-install check.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// Schema of the `boot.json` config the launcher reads on startup.
///
/// Mirrored from `launcher/src/boot.rs::BootConfig` — kept here so the
/// other Rust surfaces (Setup Assistant, menu-bar) can read the same
/// record without depending on the launcher crate directly. Field
/// optionality matches the launcher exactly.
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

/// Resolved currently-installed version of Link, as the launcher would see it.
#[derive(Debug, Clone)]
pub struct InstalledVersion {
    pub version: String,
    pub path: PathBuf,
}

/// Parse a `boot.json` from disk.
///
/// Interim implementation returns `NotFound`; the real parser lands
/// with the Setup Assistant's existing-install check. Non-panicking
/// so callers can depend on the signature today.
pub fn read_boot_json(_path: &Path) -> Result<BootConfig, std::io::Error> {
    Err(std::io::Error::new(
        std::io::ErrorKind::NotFound,
        "read_boot_json not yet implemented",
    ))
}

/// Resolve `<install_root>/current` to the version it points at.
///
/// Interim implementation returns `None` — real symlink+pointer-file
/// resolution lands with the menu-bar app's version display.
pub fn installed_version(_install_root: &Path) -> Option<InstalledVersion> {
    None
}
