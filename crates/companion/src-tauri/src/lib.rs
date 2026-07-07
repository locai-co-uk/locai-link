// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Locai Link menu-bar companion.
//!
//! Runs as a tray-only Tauri app (no dock icon, no visible window).
//! Polls the local agent's `/healthz` and `/models` endpoints on a
//! fixed cadence and reflects the state in three places:
//!
//! * The tray icon (Up/Down variants — see `TRAY_ICON_*`).
//! * A dynamic header item at the top of the tray menu ("Agent up ·
//!   v1.0.17 · idle" etc.) — updated in place via
//!   `MenuItem::set_text` on every tick.
//! * The Models submenu — one `CheckMenuItem` per servable-model
//!   pipeline. Rebuilt only when the digest of (id, is_serving)
//!   pairs shifts, so steady-state polls don't churn the menu.
//!
//! Everything the user interacts with lives in the native tray menu:
//! status header, Open Control, Models (with per-model serve toggles),
//! Quit. Quit closes the tray only — the agent is a shared service
//! other integrators depend on. Autostart is managed by the `.pkg`
//! installer / Setup Assistant, not from here.

mod preferences;

use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use locai_link_shared::{
    agent_health, list_models, toggle_serving, HealthStatus, ModelInfo, ModelsStatus, ServingAction,
    DEFAULT_HEALTH_URL, DEFAULT_MODELS_URL, DEFAULT_MODEL_ACTION_BASE,
};
use tauri::{
    image::Image,
    menu::{CheckMenuItem, IsMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::{TrayIcon, TrayIconBuilder},
    AppHandle, Manager, WindowEvent, Wry,
};
use tauri_plugin_opener::OpenerExt;

// Tray icons split by platform: macOS wants a monochrome template PNG
// (alpha channel only, no colour) so the system can auto-tint it against
// the current menu-bar appearance — hence `icon_as_template(true)` in
// the setup call below. Windows/Linux want a full-colour icon because
// the taskbar/systray background is arbitrary.
//
// All four consts point at the same 32×32 asset for now — the
// #[cfg(target_os = ...)] structure lets the design drop in a template
// variant later without a code change beyond a file swap. Down state
// (dimmer/slashed) similarly waits for the final assets.
#[cfg(target_os = "macos")]
const TRAY_ICON_UP: &[u8] = include_bytes!("../icons/32x32.png");
#[cfg(target_os = "macos")]
const TRAY_ICON_DOWN: &[u8] = include_bytes!("../icons/32x32.png");
#[cfg(not(target_os = "macos"))]
const TRAY_ICON_UP: &[u8] = include_bytes!("../icons/32x32.png");
#[cfg(not(target_os = "macos"))]
const TRAY_ICON_DOWN: &[u8] = include_bytes!("../icons/32x32.png");

/// macOS treats the tray icon as a template (auto-tinted); other
/// platforms want the icon rendered as-is.
const TRAY_ICON_IS_TEMPLATE: bool = cfg!(target_os = "macos");

/// How often the background loop asks the agent for its health.
/// UI cadence — quick enough that the tray + menu label reflect a
/// stop/start within one glance, slow enough that a hung agent doesn't
/// spam timeouts.
const POLL_INTERVAL: Duration = Duration::from_secs(5);

/// Where the "Open Control" menu item points. External URL so the
/// system browser handles it — the companion never renders the
/// Control SPA itself.
///
/// TODO(env-config): hardcoded to prod. When dev/staging need to be
/// selectable at build time, replace with a `TAURI_ENV`- or
/// `CARGO_LOCAI_ENV`-driven `env!()` lookup and a small match. Dev
/// URL is `https://dev.control.locai.co.uk`.
const CONTROL_URL: &str = "https://control.locai.co.uk";

/// Menu item IDs. Kept as const so the string is defined in exactly
/// one place — builder side (`MenuItem::with_id`) and event handler
/// (`event.id().as_ref()`) both reference these.
const MENU_ID_STATUS: &str = "status";
const MENU_ID_CONTROL: &str = "control";
const MENU_ID_MODELS_PLACEHOLDER: &str = "models_placeholder";
const MENU_ID_PREFERENCES: &str = "preferences";
const MENU_ID_QUIT: &str = "quit";

/// Prefix for per-model CheckMenuItem ids. The suffix after the prefix
/// is the pipeline id — `handle_menu_event` uses this to route
/// toggles back to the right pipeline.
const MENU_ID_MODEL_PREFIX: &str = "model:";

/// Initial label for the status header — replaced on the first poll
/// response, at most 5s later.
const STATUS_INITIAL: &str = "Locai Link — checking agent…";

/// Coarse categorisation used to decide whether the tray icon needs
/// to change. Two-valued because Malformed and Down present the same
/// way to the user — the agent isn't giving us usable data.
#[derive(Copy, Clone, PartialEq, Eq)]
enum TrayState {
    Up,
    Down,
}

impl From<&HealthStatus> for TrayState {
    fn from(status: &HealthStatus) -> Self {
        match status {
            HealthStatus::Up(_) => TrayState::Up,
            HealthStatus::Down | HealthStatus::Malformed(_) => TrayState::Down,
        }
    }
}

/// Handles that the poll loop mutates in place across ticks. The
/// status header is set-text on every tick; the whole menu is
/// rebuilt (and installed on the tray) only when the models list
/// changes, per the digest check in [`models_digest`].
///
/// `models` is the last-known list. The click handler reads it to
/// figure out the current `is_serving` state and choose start vs stop
/// without having to fire an extra GET on the click path.
struct MenuHandles {
    status_item: MenuItem<Wry>,
    models: Vec<ModelInfo>,
}

/// Shared type registered in Tauri app state so both the poll loop
/// (writer) and the menu-event handler (reader) can reach the same
/// MenuHandles.
type SharedHandles = Arc<Mutex<MenuHandles>>;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            preferences::get_prefs_state,
            preferences::poll_status,
            preferences::set_run_at_login,
            preferences::runtime_start,
            preferences::runtime_stop,
            preferences::runtime_restart,
            preferences::reveal_log_file,
            preferences::open_control_device,
            preferences::launch_uninstaller_prefs,
        ])
        // Prevent close-button from exiting the app — the tray keeps
        // running, only the Preferences window is hidden. On macOS
        // flip the activation policy back to Accessory so we drop the
        // Dock icon while no window is visible.
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
                #[cfg(target_os = "macos")]
                {
                    let _ = window
                        .app_handle()
                        .set_activation_policy(tauri::ActivationPolicy::Accessory);
                }
            }
        })
        .setup(|app| {
            // macOS: mark this as an accessory (menu-bar) app so it
            // doesn't take a Dock slot, doesn't appear in Cmd-Tab, and
            // doesn't steal focus at launch. `visible: false` +
            // `skipTaskbar: true` in tauri.conf.json aren't enough on
            // macOS — the AppKit activation policy is the load-bearing
            // switch.
            #[cfg(target_os = "macos")]
            let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            // If the runtime LaunchAgent is bootstrapped but stopped
            // (user hard-quit last session), kickstart brings it back.
            // If it's already running, this is a no-op (`kickstart`
            // without `-k` doesn't restart a live service). If the
            // LaunchAgent isn't bootstrapped at all (user opted out of
            // "Start at login"), the call silently fails and the poll
            // loop will show "Down" — that's the correct outcome.
            //
            // Backgrounded so we don't block the tray icon appearing.
            #[cfg(target_os = "macos")]
            thread::spawn(kickstart_runtime_if_installed);

            let (menu, status_item) = build_tray_menu(app.handle(), &[], STATUS_INITIAL)?;

            let icon = Image::from_bytes(TRAY_ICON_UP)?;
            let tray = TrayIconBuilder::with_id("main")
                .icon(icon)
                .icon_as_template(TRAY_ICON_IS_TEMPLATE)
                .tooltip("Locai Link")
                .menu(&menu)
                // macOS convention is left-click → menu (no separate
                // left/right split); mirror it on every platform so
                // there's one interaction to learn.
                .show_menu_on_left_click(true)
                .on_menu_event(handle_menu_event)
                .build(app)?;

            // Handles the poll loop mutates. Wrapped in Arc<Mutex<_>>
            // so the loop can swap the status_item reference when the
            // menu is rebuilt with a fresh one. Also registered as
            // Tauri app state so `handle_menu_event` can reach the
            // current models list.
            let handles: SharedHandles = Arc::new(Mutex::new(MenuHandles {
                status_item,
                models: Vec::new(),
            }));
            app.manage(handles.clone());
            let app_handle = app.handle().clone();
            thread::spawn(move || poll_forever(app_handle, tray, handles));
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

/// Kick the runtime LaunchAgent so the user's "open Locai Link"
/// gesture (via `/Applications/`, Spotlight, Launchpad) is also
/// implicitly "start Locai Link if it's down".
///
/// `launchctl kickstart` (without `-k`) starts a service if it's not
/// running and no-ops if it is. If the LaunchAgent isn't bootstrapped
/// at all (user opted out via System Settings → Login Items), the
/// call fails silently — the tray poll loop will just show a "Down"
/// state until the user re-enables the agent.
///
/// Runs on macOS only; other platforms don't have launchctl.
#[cfg(target_os = "macos")]
fn kickstart_runtime_if_installed() {
    let uid = match std::process::Command::new("id").arg("-u").output() {
        Ok(o) if o.status.success() => {
            String::from_utf8_lossy(&o.stdout).trim().to_string()
        }
        _ => return,
    };
    let service = format!("gui/{uid}/uk.co.locai.link.agent");
    let _ = std::process::Command::new("launchctl")
        .args(["kickstart", &service])
        .output();
}

/// Show + focus the Preferences window from a tray menu click. If the
/// user closed it earlier the window is hidden (see `on_window_event`
/// in `run()`), so this both un-hides and pulls focus. Errors are
/// non-fatal — logged and swallowed so a UI hiccup doesn't kill the
/// menu event thread.
fn show_preferences_window(app: &AppHandle) {
    let Some(window) = app.get_webview_window("main") else {
        eprintln!("[companion] preferences window not found");
        return;
    };
    if let Err(e) = window.show() {
        eprintln!("[companion] window.show failed: {e}");
        return;
    }
    if let Err(e) = window.set_focus() {
        eprintln!("[companion] window.set_focus failed: {e}");
    }
    // macOS: while the window is visible we want the app to behave
    // like a regular foreground app (Dock icon, Cmd-Tab entry, focus
    // handling). Flip back to Accessory when it hides — see
    // `on_window_event`.
    #[cfg(target_os = "macos")]
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);
}

/// Assemble the tray menu and return it along with a handle to the
/// freshly-created status header — the poll loop calls `set_text` on
/// that handle every tick so the header always reflects the latest
/// snapshot. Rebuilt (from scratch) only when the models list changes.
///
/// Structure:
/// ```text
/// <status header>              (disabled — informational only)
/// ─────────
/// Open Control
/// Models →
///     ☑/☐ <alias>·:port        (one CheckMenuItem per model)
///     …
/// ─────────
/// Quit                         Cmd/Ctrl+Q
/// ```
///
/// Quit closes the companion only — the agent runs as a shared
/// service that other integrator apps (SafeChat, Meetily) depend on,
/// so a tray-app quit must not stop it. Autostart is managed by the
/// `.pkg` installer + Setup Assistant, not by a companion toggle.
fn build_tray_menu(
    app: &AppHandle,
    models: &[ModelInfo],
    status_text: &str,
) -> tauri::Result<(Menu<Wry>, MenuItem<Wry>)> {
    // `enabled: false` gives macOS/Windows/Linux their native "grey
    // text, unclickable" rendering used for informational items.
    let status = MenuItem::with_id(app, MENU_ID_STATUS, status_text, false, None::<&str>)?;
    let sep1 = PredefinedMenuItem::separator(app)?;

    let control = MenuItem::with_id(app, MENU_ID_CONTROL, "Open Control", true, None::<&str>)?;
    let models_submenu = build_models_submenu(app, models)?;

    let sep2 = PredefinedMenuItem::separator(app)?;
    // Preferences opens the settings window (Device / Agent / Network
    // / Advanced). Uninstall lives *inside* that window in the Advanced
    // panel — it used to sit here on the tray, moved so all destructive
    // + configuration actions share one surface.
    let preferences = MenuItem::with_id(
        app,
        MENU_ID_PREFERENCES,
        "Preferences…",
        true,
        Some("CmdOrCtrl+,"),
    )?;
    let quit = MenuItem::with_id(app, MENU_ID_QUIT, "Quit", true, Some("CmdOrCtrl+Q"))?;

    let menu = Menu::with_items(
        app,
        &[&status, &sep1, &control, &models_submenu, &sep2, &preferences, &quit],
    )?;
    Ok((menu, status))
}

/// Populate the Models submenu. Empty model list → single disabled
/// placeholder ("(no models configured)"); populated list → one
/// `CheckMenuItem` per model, checked iff currently serving. Toggles
/// are wired in task 42c.
fn build_models_submenu(app: &AppHandle, models: &[ModelInfo]) -> tauri::Result<Submenu<Wry>> {
    if models.is_empty() {
        let placeholder = MenuItem::with_id(
            app,
            MENU_ID_MODELS_PLACEHOLDER,
            "(no models configured)",
            false,
            None::<&str>,
        )?;
        return Submenu::with_id_and_items(app, "models", "Models", true, &[&placeholder]);
    }

    // Own the CheckMenuItems so we can hand references into the
    // Submenu builder — items must outlive the builder call.
    let items: Vec<CheckMenuItem<Wry>> = models
        .iter()
        .map(|m| {
            CheckMenuItem::with_id(
                app,
                format!("{MENU_ID_MODEL_PREFIX}{}", m.id),
                model_label(m),
                true,
                m.is_serving,
                None::<&str>,
            )
        })
        .collect::<tauri::Result<Vec<_>>>()?;

    let refs: Vec<&dyn IsMenuItem<Wry>> = items.iter().map(|i| i as &dyn IsMenuItem<Wry>).collect();
    Submenu::with_id_and_items(app, "models", "Models", true, &refs)
}

/// Label for a single model row in the submenu. Includes the port
/// suffix always so users with mixed-port setups can distinguish
/// llama-swap instances.
fn model_label(m: &ModelInfo) -> String {
    match m.port {
        Some(port) => format!("{} · :{}", m.alias, port),
        None => m.alias.clone(),
    }
}

/// Stable digest of the models list. Any change in count, ordering,
/// id, or is_serving state flips the digest — everything else (port
/// rename, alias tweak) doesn't trigger a menu rebuild.
fn models_digest(models: &[ModelInfo]) -> Vec<(String, bool)> {
    let mut d: Vec<(String, bool)> = models.iter().map(|m| (m.id.clone(), m.is_serving)).collect();
    d.sort();
    d
}

fn handle_menu_event(app: &AppHandle, event: tauri::menu::MenuEvent) {
    let id = event.id().as_ref();
    match id {
        MENU_ID_CONTROL => {
            // `None::<&str>` = launch with the user's default browser
            // (no explicit "program" override). Errors reduce to a log
            // line because a menu click that fails to open a browser
            // isn't worth a crash.
            if let Err(e) = app.opener().open_url(CONTROL_URL, None::<&str>) {
                eprintln!("[companion] failed to open {CONTROL_URL}: {e}");
            }
        }
        MENU_ID_PREFERENCES => {
            show_preferences_window(app);
        }
        MENU_ID_QUIT => {
            // Close the tray only. The agent is a shared service that
            // integrator apps (SafeChat, Meetily) depend on — quitting
            // the companion must not stop it. Users who want to stop
            // the agent do it via Control's web UI.
            app.exit(0);
        }
        _ if id.starts_with(MENU_ID_MODEL_PREFIX) => {
            let pipeline_id = id[MENU_ID_MODEL_PREFIX.len()..].to_string();
            handle_model_toggle(app, pipeline_id);
        }
        // Status header + Models placeholder are both disabled — they
        // shouldn't fire events, but if they do somehow, ignore.
        MENU_ID_STATUS | MENU_ID_MODELS_PLACEHOLDER => {}
        other => eprintln!("[companion] unhandled menu id: {other}"),
    }
}

/// User clicked a model checkbox. Look up the pipeline's current
/// `is_serving` in the last-known models list, pick the opposite
/// action, fire the POST on a detached thread so the menu event
/// handler returns immediately (Tauri's menu-event thread is a
/// UI-adjacent thread — a blocking HTTP call there would freeze the
/// menu).
fn handle_model_toggle(app: &AppHandle, pipeline_id: String) {
    let handles: tauri::State<'_, SharedHandles> = app.state();
    let action = match handles.lock() {
        Ok(guard) => guard.models.iter().find(|m| m.id == pipeline_id).map(|m| {
            if m.is_serving {
                ServingAction::Stop
            } else {
                ServingAction::Start
            }
        }),
        Err(e) => {
            eprintln!("[companion] handles mutex poisoned: {e}");
            return;
        }
    };
    let Some(action) = action else {
        eprintln!("[companion] click on unknown pipeline: {pipeline_id}");
        return;
    };

    thread::spawn(move || {
        if let Err(e) = toggle_serving(DEFAULT_MODEL_ACTION_BASE, &pipeline_id, action) {
            eprintln!("[companion] toggle_serving({pipeline_id}, {action:?}) failed: {e}");
        }
        // Success or failure, the next poll (up to POLL_INTERVAL
        // later) reads /models and reconciles the checkbox state.
        // No optimistic UI update — polling is authoritative.
    });
}

/// Poll both `/healthz` and `/models` on a fixed cadence.
///
/// Tray icon swaps on Up↔Down transitions only. Status header text is
/// rewritten every tick so uptime refreshes during a steady state. The
/// full menu is rebuilt only when the models digest changes (add,
/// remove, or serve-toggle) — steady-state polls don't touch the menu
/// structure at all.
///
/// Runs on a dedicated OS thread rather than a Tokio task — `ureq` is
/// blocking, we're not paying for async elsewhere in the companion,
/// and one long-lived thread is fine.
fn poll_forever(app: AppHandle, tray: TrayIcon, handles: Arc<Mutex<MenuHandles>>) {
    let mut current_tray = TrayState::Up;
    let mut current_digest: Vec<(String, bool)> = Vec::new();

    loop {
        let health = agent_health(DEFAULT_HEALTH_URL);
        let models = list_models(DEFAULT_MODELS_URL);

        // Tray icon (up/down transitions only)
        let next_tray = TrayState::from(&health);
        if next_tray != current_tray {
            let bytes = match next_tray {
                TrayState::Up => TRAY_ICON_UP,
                TrayState::Down => TRAY_ICON_DOWN,
            };
            if let Ok(img) = Image::from_bytes(bytes) {
                if let Err(e) = tray.set_icon(Some(img)) {
                    eprintln!("[companion] tray.set_icon failed: {e}");
                }
            }
            current_tray = next_tray;
        }

        let status_text = status_label(&health, &models);

        // Menu rebuild is expensive-ish (allocates MenuItems, calls
        // into the platform menu API); only do it when the digest
        // actually shifts. If /models is Down or Malformed, treat the
        // list as empty rather than clearing on transient network
        // hiccups — this avoids flapping the menu when the agent's
        // /models path is briefly unavailable but /healthz still
        // works. TODO: dedicated "stale" state that keeps last-known.
        let new_models: Vec<ModelInfo> = match &models {
            ModelsStatus::Ok(list) => list.clone(),
            ModelsStatus::Down | ModelsStatus::Malformed(_) => Vec::new(),
        };
        let new_digest = models_digest(&new_models);

        if new_digest != current_digest {
            match build_tray_menu(&app, &new_models, &status_text) {
                Ok((new_menu, new_status_item)) => {
                    if let Err(e) = tray.set_menu(Some(new_menu)) {
                        eprintln!("[companion] tray.set_menu failed: {e}");
                    }
                    // Point the status handle at the freshly-created
                    // header so subsequent set_text calls land on the
                    // menu that's actually mounted, and stash the
                    // models list for the click handler.
                    if let Ok(mut h) = handles.lock() {
                        h.status_item = new_status_item;
                        h.models = new_models.clone();
                    }
                    current_digest = new_digest;
                }
                Err(e) => eprintln!("[companion] build_tray_menu failed: {e}"),
            }
        } else {
            // Steady state: push the fresh status text into the
            // existing header, and keep the stored models list in
            // sync (aliases/ports may have shifted without flipping
            // the digest).
            if let Ok(mut h) = handles.lock() {
                if let Err(e) = h.status_item.set_text(&status_text) {
                    eprintln!("[companion] status_item.set_text failed: {e}");
                }
                h.models = new_models;
            }
        }

        thread::sleep(POLL_INTERVAL);
    }
}

/// Compact one-line summary for the status header. Kept short because
/// tray menus don't wrap.
///
/// Serving state is derived from `/models` (not `/healthz.currently_serving`)
/// because `currently_serving` is a single bool set only by explicit
/// StartServingCommand — it misses inline START_MODEL + serve-mode
/// configs and can't represent multi-model llama-swap deployments.
/// `/models[].is_serving` is per-pipeline truth.
fn status_label(health: &HealthStatus, models: &ModelsStatus) -> String {
    match health {
        HealthStatus::Up(h) => {
            let state = match models {
                ModelsStatus::Ok(list) => {
                    let serving: Vec<&ModelInfo> = list.iter().filter(|m| m.is_serving).collect();
                    match serving.len() {
                        0 => "idle".to_string(),
                        1 => format!("serving {}", serving[0].alias),
                        n => format!("serving {n} models"),
                    }
                }
                // /models unavailable → drop the serving-state suffix
                // rather than misreporting as idle. Rare — happens
                // during startup or a transient hiccup.
                ModelsStatus::Down | ModelsStatus::Malformed(_) => return format!("Agent up · v{}", h.version),
            };
            format!("Agent up · v{} · {}", h.version, state)
        }
        HealthStatus::Down => "Agent down".to_string(),
        HealthStatus::Malformed(_) => "Agent responded with malformed data".to_string(),
    }
}
