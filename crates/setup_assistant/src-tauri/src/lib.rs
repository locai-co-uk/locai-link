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
    /// Human-readable reason when `installed` is false. `None` on the
    /// success path.
    pub reason: Option<String>,
}

/// Read the on-disk install state at `install_root`.
///
/// Never returns an `Err` — the failure modes ("no install here",
/// "install root doesn't exist") are legitimate outcomes the UI needs
/// to render, not exceptions. The `reason` field carries the detail.
#[tauri::command]
fn check_install(install_root: String) -> CheckInstallResult {
    let root = PathBuf::from(&install_root);
    if !root.exists() {
        return CheckInstallResult {
            installed: false,
            version: None,
            path: None,
            boot: None,
            reason: Some(format!("Install root does not exist: {install_root}")),
        };
    }

    let boot = read_boot_json(&root.join("boot.json")).ok();

    match installed_version(&root) {
        Some(v) => CheckInstallResult {
            installed: true,
            version: Some(v.version),
            path: Some(v.path.to_string_lossy().into_owned()),
            boot,
            reason: None,
        },
        None => CheckInstallResult {
            installed: false,
            version: None,
            path: None,
            boot,
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
