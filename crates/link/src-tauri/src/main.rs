// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

// Prevents an extra console window on Windows in release for the desktop app.
// The headless supervisor is a service binary and keeps a console.
#![cfg_attr(
    all(not(debug_assertions), feature = "ui"),
    windows_subsystem = "windows"
)]

/// How the process was launched, decided from the first CLI arg. `locai` (the
/// CLI symlink) and the service unit share this one binary.
#[derive(Debug, PartialEq, Eq)]
enum Launch {
    /// `run` = the long-lived supervised service (idles until registered).
    Service,
    /// A lifecycle op handled natively (start/stop/restart/uninstall), shared
    /// with the desktop shape; carries the subcommand name.
    Lifecycle(&'static str),
    /// A runtime subcommand (register/status/update/…) = one-shot passthrough.
    OneShot,
    /// No-arg or flag-style launch (e.g. `-psn_…`) = the desktop app.
    App,
}

fn classify_launch(first: Option<&str>) -> Launch {
    match first {
        Some("run") => Launch::Service,
        Some("start") => Launch::Lifecycle("start"),
        Some("stop") => Launch::Lifecycle("stop"),
        Some("restart") => Launch::Lifecycle("restart"),
        Some("uninstall") => Launch::Lifecycle("uninstall"),
        Some(arg) if !arg.starts_with('-') => Launch::OneShot,
        _ => Launch::App,
    }
}

// Desktop: the Tauri app, which also drives the supervisor thread. The `locai`
// CLI symlink points at this binary, so `locai run` supervises the runtime and
// other subcommands run one-shot, rather than popping the GUI.
#[cfg(feature = "ui")]
fn main() -> std::process::ExitCode {
    let first = std::env::args_os().nth(1);
    let first = first.as_ref().map(|a| a.to_string_lossy());
    match classify_launch(first.as_deref()) {
        Launch::Service => link_lib::supervisor::run_service(),
        Launch::Lifecycle(cmd) => link_lib::lifecycle::run(cmd),
        Launch::OneShot => link_lib::supervisor::run_supervisor(),
        Launch::App => {
            link_lib::run();
            std::process::ExitCode::SUCCESS
        }
    }
}

// Headless: `run` is the supervised service; everything else (there is no desktop
// app) is a one-shot passthrough.
#[cfg(not(feature = "ui"))]
fn main() -> std::process::ExitCode {
    let first = std::env::args_os().nth(1);
    let first = first.as_ref().map(|a| a.to_string_lossy());
    match classify_launch(first.as_deref()) {
        Launch::Service => link_lib::supervisor::run_service(),
        Launch::Lifecycle(cmd) => link_lib::lifecycle::run(cmd),
        _ => link_lib::supervisor::run_supervisor(),
    }
}

#[cfg(test)]
mod tests {
    use super::{classify_launch, Launch};

    #[test]
    fn run_is_the_service() {
        assert_eq!(classify_launch(Some("run")), Launch::Service);
    }

    #[test]
    fn runtime_subcommands_are_one_shot() {
        for cmd in ["register", "status", "update", "install-plugin", "self-check", "reset"] {
            assert_eq!(classify_launch(Some(cmd)), Launch::OneShot, "{cmd}");
        }
    }

    #[test]
    fn lifecycle_subcommands_are_native() {
        for cmd in ["start", "stop", "restart", "uninstall"] {
            assert_eq!(classify_launch(Some(cmd)), Launch::Lifecycle(cmd), "{cmd}");
        }
    }

    #[test]
    fn no_arg_or_flags_are_the_app() {
        assert_eq!(classify_launch(None), Launch::App);
        assert_eq!(classify_launch(Some("-psn_0_12345")), Launch::App);
        assert_eq!(classify_launch(Some("--help")), Launch::App);
    }
}
