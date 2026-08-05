// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

// Prevents an extra console window on Windows in release for the desktop app.
// The headless supervisor is a service binary and keeps a console.
#![cfg_attr(
    all(not(debug_assertions), feature = "ui"),
    windows_subsystem = "windows"
)]

// Desktop: the Tauri app, which also drives the supervisor thread.
// The `locai` CLI symlink points at this binary, so a subcommand invocation
// (`locai run`) supervises the runtime in the foreground rather than popping
// the GUI. A no-arg launch and flag-style args (`-psn_…`) fall through to the
// desktop app.
#[cfg(feature = "ui")]
fn main() -> std::process::ExitCode {
    let is_cli = std::env::args_os()
        .nth(1)
        .is_some_and(|a| !a.to_string_lossy().starts_with('-'));
    if is_cli {
        return link_lib::supervisor::run_supervisor();
    }
    link_lib::run();
    std::process::ExitCode::SUCCESS
}

// Headless: just the supervisor loop.
#[cfg(not(feature = "ui"))]
fn main() -> std::process::ExitCode {
    link_lib::supervisor::run_supervisor()
}
