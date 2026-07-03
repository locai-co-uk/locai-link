// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

use std::path::PathBuf;

use serde::Serialize;

use locai_link_shared::{installed_version, read_boot_json, BootConfig};

/// Wire-format result for the Setup Assistant's "Check Install" step.
///
/// Kept flat and JSON-friendly so the Svelte side can consume it
/// without a schema library. `path` is a stringified `PathBuf` — the
/// frontend only shows it, never operates on it.
#[derive(Serialize)]
pub struct CheckInstallResult {
    pub installed: bool,
    pub version: Option<String>,
    pub path: Option<String>,
    pub boot: Option<BootConfig>,
    /// Populated when `boot.json` exists but couldn't be parsed. Kept
    /// distinct from `reason` so the UI can say "install found but
    /// config is broken" rather than hiding the failure.
    pub boot_error: Option<String>,
    /// Human-readable reason when `installed` is false. `None` on the
    /// success path.
    pub reason: Option<String>,
}

/// Read the on-disk install state at `install_root`.
///
/// Never returns an `Err` — the failure modes ("no install here",
/// "install root doesn't exist", "boot.json corrupt") are legitimate
/// outcomes the UI needs to render, not exceptions. `reason` /
/// `boot_error` carry the detail.
#[tauri::command]
fn check_install(install_root: String) -> CheckInstallResult {
    let root = PathBuf::from(&install_root);
    if !root.exists() {
        return CheckInstallResult {
            installed: false,
            version: None,
            path: None,
            boot: None,
            boot_error: None,
            reason: Some(format!("Install root does not exist: {install_root}")),
        };
    }

    let boot_path = root.join("boot.json");
    let (boot, boot_error) = if boot_path.exists() {
        match read_boot_json(&boot_path) {
            Ok(cfg) => (Some(cfg), None),
            Err(e) => (None, Some(format!("boot.json unreadable: {e}"))),
        }
    } else {
        (None, None)
    };

    match installed_version(&root) {
        Some(v) => CheckInstallResult {
            installed: true,
            version: Some(v.version),
            path: Some(v.path.to_string_lossy().into_owned()),
            boot,
            boot_error,
            reason: None,
        },
        None => CheckInstallResult {
            installed: false,
            version: None,
            path: None,
            boot,
            boot_error,
            reason: Some("No `current` pointer found under install root.".to_string()),
        },
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![check_install])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
