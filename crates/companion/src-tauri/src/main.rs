// Prevents an extra console window on Windows in release for the desktop app.
// The headless supervisor is a service binary and keeps a console.
#![cfg_attr(
    all(not(debug_assertions), feature = "ui"),
    windows_subsystem = "windows"
)]

// Desktop: the Tauri app (which, from P2, also drives the supervisor thread).
#[cfg(feature = "ui")]
fn main() {
    companion_lib::run()
}

// Headless: just the supervisor loop (resolve current, spawn + supervise the
// runtime child, exit-42 respawn, rollback, bootstrap).
#[cfg(not(feature = "ui"))]
fn main() -> std::process::ExitCode {
    companion_lib::supervisor::run_supervisor()
}
