// Prevents an extra console window on Windows in release for the desktop app.
// The headless supervisor is a service binary and keeps a console.
#![cfg_attr(
    all(not(debug_assertions), feature = "ui"),
    windows_subsystem = "windows"
)]

// Desktop: the Tauri app (which, from P2, also drives the supervisor thread).
// The `locai` CLI symlink points at this binary, so a terminal invocation with
// a subcommand (`locai run`) must behave like the headless binary — supervise
// the runtime in the foreground — not pop the GUI. A no-arg launch (the
// service / Finder) and flag-style args from Launch Services (`-psn_…`) fall
// through to the desktop app.
#[cfg(feature = "ui")]
fn main() -> std::process::ExitCode {
    let is_cli = std::env::args_os()
        .nth(1)
        .is_some_and(|a| !a.to_string_lossy().starts_with('-'));
    if is_cli {
        return companion_lib::supervisor::run_supervisor();
    }
    companion_lib::run();
    std::process::ExitCode::SUCCESS
}

// Headless: just the supervisor loop (resolve current, spawn + supervise the
// runtime child, exit-42 respawn, rollback, bootstrap).
#[cfg(not(feature = "ui"))]
fn main() -> std::process::ExitCode {
    companion_lib::supervisor::run_supervisor()
}
