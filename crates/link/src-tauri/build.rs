fn main() {
    // tauri_build only applies to the desktop (`ui`) build; the headless
    // supervisor binary has no Tauri context to generate.
    if std::env::var_os("CARGO_FEATURE_UI").is_some() {
        tauri_build::build();
    }
}
