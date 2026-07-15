// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Locai Link menu-bar companion — tray-only Tauri app that polls the local
//! agent's `/healthz` and `/models` and reflects state in the tray icon,
//! header text, and Models submenu.

mod preferences;

use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use locai_link_shared::{
    agent_health, list_models, toggle_serving, trigger_update, DeploymentProgress, HealthStatus,
    ModelInfo, ModelsStatus, ServingAction, DEFAULT_HEALTH_URL, DEFAULT_MODELS_URL,
    DEFAULT_MODEL_ACTION_BASE, DEFAULT_UPDATE_URL,
};
use tauri::{
    image::Image,
    menu::{CheckMenuItem, IsMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::{TrayIcon, TrayIconBuilder},
    AppHandle, Emitter, Manager, WindowEvent, Wry,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;

// All four consts point at the same 32×32 asset for now — cfg structure
// lets the design drop in template + Down variants later without code change.
#[cfg(target_os = "macos")]
const TRAY_ICON_UP: &[u8] = include_bytes!("../icons/32x32.png");
#[cfg(target_os = "macos")]
const TRAY_ICON_DOWN: &[u8] = include_bytes!("../icons/32x32.png");
#[cfg(not(target_os = "macos"))]
const TRAY_ICON_UP: &[u8] = include_bytes!("../icons/32x32.png");
#[cfg(not(target_os = "macos"))]
const TRAY_ICON_DOWN: &[u8] = include_bytes!("../icons/32x32.png");

/// The brand tray icon is a filled badge not a monochrome glyph
const TRAY_ICON_IS_TEMPLATE: bool = false;

const POLL_INTERVAL: Duration = Duration::from_secs(5);

// TODO(env-config): hardcoded to prod; wire dev/staging via env!() when needed.
const CONTROL_URL: &str = "https://control.locai.co.uk";

const MENU_ID_STATUS: &str = "status";
const MENU_ID_CONTROL: &str = "control";
const MENU_ID_MODELS_PLACEHOLDER: &str = "models_placeholder";
const MENU_ID_PREFERENCES: &str = "preferences";
const MENU_ID_DOWNLOAD: &str = "download_models";
const MENU_ID_QUIT: &str = "quit";
const MENU_ID_UPDATE: &str = "update";

/// Emitted to the Preferences window when the user picks "Download models…" so
/// the UI opens on the available-models section.
const EVENT_SHOW_DOWNLOADS: &str = "show-downloads";

/// Suffix after this prefix is the pipeline id.
const MENU_ID_MODEL_PREFIX: &str = "model:";

/// Release-channel suffix baked in from VITE_CHANNEL at compile time
/// (defaults to "alpha" when unset — matches the SA side). "prod" or
/// empty → the suffix is empty and no channel marker renders.
static CHANNEL_SUFFIX: std::sync::LazyLock<String> = std::sync::LazyLock::new(|| {
    let raw = option_env!("VITE_CHANNEL")
        .unwrap_or("alpha")
        .to_ascii_lowercase();
    if raw.is_empty() || raw == "prod" {
        String::new()
    } else {
        let mut chars = raw.chars();
        let capitalized: String = chars
            .next()
            .map(|c| c.to_ascii_uppercase())
            .into_iter()
            .chain(chars)
            .collect();
        format!(" · {capitalized}")
    }
});

fn status_text_initial() -> String {
    format!("Locai Link{} · Checking…", *CHANNEL_SUFFIX)
}
fn status_text_up() -> String {
    format!("Locai Link{} · ONLINE", *CHANNEL_SUFFIX)
}
fn status_text_down() -> String {
    format!("Locai Link{} · OFFLINE", *CHANNEL_SUFFIX)
}
fn status_text_updating() -> String {
    format!("Locai Link{} · UPDATING", *CHANNEL_SUFFIX)
}

/// Malformed and Down collapse into `Down` — both mean "no usable data".
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

/// Mutated in place by the poll loop; the click handler reads `models` to
/// pick start-vs-stop without an extra GET on the click path.
/// `in_flight` blocks re-clicks on a pipeline while its Serve/Stop HTTP is
/// outstanding — a rapid stop→start clobbered the runtime otherwise.
struct MenuHandles {
    status_item: MenuItem<Wry>,
    models: Vec<ModelInfo>,
    in_flight: std::collections::HashSet<String>,
    /// Blocks repeat "Update" clicks while a swap is in flight. Held from the
    /// click until poll_forever sees the update finish (or on POST failure).
    update_in_flight: bool,
}

type SharedHandles = Arc<Mutex<MenuHandles>>;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            preferences::get_prefs_state,
            preferences::poll_status,
            preferences::toggle_model_serving,
            preferences::cancel_model_deploy,
            preferences::list_available_models,
            preferences::request_model_deploy,
            preferences::supported_model_types,
            preferences::install_update,
            preferences::set_run_at_login,
            preferences::runtime_start,
            preferences::runtime_stop,
            preferences::runtime_restart,
            preferences::reveal_log_file,
            preferences::open_control_device,
        ])
        // Close-button hides Preferences; tray keeps running. macOS: flip back
        // to Accessory so the Dock icon drops while no window is visible.
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
            // Accessory activation policy is the load-bearing switch that keeps
            // this off the Dock and out of Cmd-Tab; tauri.conf's `visible: false`
            // isn't enough on macOS.
            #[cfg(target_os = "macos")]
            let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            // Backgrounded so we don't block the tray icon appearing.
            #[cfg(target_os = "macos")]
            thread::spawn(kickstart_runtime_if_installed);

            let (menu, status_item) =
                build_tray_menu(app.handle(), &[], &[], &status_text_initial(), None)?;

            let icon = Image::from_bytes(TRAY_ICON_UP)?;
            let tray = TrayIconBuilder::with_id("main")
                .icon(icon)
                .icon_as_template(TRAY_ICON_IS_TEMPLATE)
                .tooltip("Locai Link")
                .menu(&menu)
                .show_menu_on_left_click(true)
                .on_menu_event(handle_menu_event)
                .build(app)?;

            let handles: SharedHandles = Arc::new(Mutex::new(MenuHandles {
                status_item,
                models: Vec::new(),
                in_flight: std::collections::HashSet::new(),
                update_in_flight: false,
            }));
            app.manage(handles.clone());
            let app_handle = app.handle().clone();
            thread::spawn(move || poll_forever(app_handle, tray, handles));

            let ipc_handle = app.handle().clone();
            thread::spawn(move || spawn_ipc_listener(ipc_handle));
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

/// Kick the runtime LaunchAgent so "open Locai Link" implicitly starts it if down.
/// `kickstart` (no `-k`) is a no-op on a live service and silently fails when the
/// agent isn't bootstrapped.
#[cfg(target_os = "macos")]
fn kickstart_runtime_if_installed() {
    let uid = match std::process::Command::new("id").arg("-u").output() {
        Ok(o) if o.status.success() => String::from_utf8_lossy(&o.stdout).trim().to_string(),
        _ => return,
    };
    let service = format!("gui/{uid}/uk.co.locai.link.agent");
    let _ = std::process::Command::new("launchctl")
        .args(["kickstart", &service])
        .output();
}

/// Companion IPC port, adjacent to the health server's 20505. Both live
/// below the ephemeral range floor (32768 on Linux / 49152 on macOS) so the
/// OS can't grab them for an outgoing connection before we bind.
const IPC_PORT: u16 = 20506;

/// Loopback listener so other processes can ask the companion to open
/// Preferences. One endpoint: `POST /preferences/show` → 204; anything else → 404.
fn spawn_ipc_listener(app: AppHandle) {
    use std::io::{BufRead, BufReader, Write};
    use std::net::TcpListener;

    let listener = match TcpListener::bind(("127.0.0.1", IPC_PORT)) {
        Ok(l) => l,
        Err(e) => {
            // Non-fatal — tray still works; SA falls through to starting the service.
            eprintln!("[companion] IPC listener bind {IPC_PORT}: {e}");
            return;
        }
    };

    for incoming in listener.incoming() {
        let mut stream = match incoming {
            Ok(s) => s,
            Err(_) => continue,
        };
        let mut reader = BufReader::new(&stream);
        let mut request_line = String::new();
        if reader.read_line(&mut request_line).is_err() {
            continue;
        }
        // Drain remaining headers to avoid RST-ing the client mid-write.
        let mut header = String::new();
        while reader
            .read_line(&mut header)
            .map(|n| n > 2)
            .unwrap_or(false)
        {
            header.clear();
        }

        let is_show = request_line.starts_with("POST /preferences/show ")
            || request_line.starts_with("POST /preferences/show?");
        let (status_line, do_show) = if is_show {
            ("HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n", true)
        } else {
            ("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n", false)
        };
        let _ = stream.write_all(status_line.as_bytes());
        let _ = stream.flush();

        if do_show {
            // Tauri window ops must run on the main thread.
            let handle = app.clone();
            let _ = app.run_on_main_thread(move || show_preferences_window(&handle));
        }
    }
}

/// Show + focus the Preferences window; un-hides if the user closed it earlier.
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
    // macOS: switch to Regular while visible so we get Dock + Cmd-Tab + focus.
    // Flipped back to Accessory in on_window_event when the window hides.
    #[cfg(target_os = "macos")]
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);
}

/// Assemble the tray menu; returns a handle to the status header so the
/// poll loop can `set_text` it every tick. Rebuilt only when the models list changes.
fn build_tray_menu(
    app: &AppHandle,
    models: &[ModelInfo],
    deployments: &[DeploymentProgress],
    status_text: &str,
    update: Option<&str>,
) -> tauri::Result<(Menu<Wry>, MenuItem<Wry>)> {
    // `enabled: false` renders as grey/unclickable — informational only.
    let status = MenuItem::with_id(app, MENU_ID_STATUS, status_text, false, None::<&str>)?;
    let sep1 = PredefinedMenuItem::separator(app)?;

    let models_submenu = build_models_submenu(app, models, deployments)?;

    let sep2 = PredefinedMenuItem::separator(app)?;
    let control = MenuItem::with_id(
        app,
        MENU_ID_CONTROL,
        "Open Control Plane",
        true,
        None::<&str>,
    )?;
    let download = MenuItem::with_id(
        app,
        MENU_ID_DOWNLOAD,
        "Download models…",
        true,
        None::<&str>,
    )?;
    let preferences = MenuItem::with_id(
        app,
        MENU_ID_PREFERENCES,
        "Preferences…",
        true,
        Some("CmdOrCtrl+,"),
    )?;
    let quit = MenuItem::with_id(app, MENU_ID_QUIT, "Quit", true, Some("CmdOrCtrl+Q"))?;

    // "Update to vX.Y.Z" appears only when the agent reports one available.
    let update_item = match update {
        Some(v) => Some(MenuItem::with_id(
            app,
            MENU_ID_UPDATE,
            format!("Update to v{v}"),
            true,
            None::<&str>,
        )?),
        None => None,
    };

    let mut items: Vec<&dyn IsMenuItem<Wry>> = vec![
        &status as &dyn IsMenuItem<Wry>,
        &sep1,
        &models_submenu,
        &sep2,
    ];
    if let Some(u) = &update_item {
        items.push(u);
    }
    items.push(&control);
    items.push(&download);
    items.push(&preferences);
    items.push(&quit);

    let menu = Menu::with_items(app, &items)?;
    Ok((menu, status))
}

/// Populate the Models submenu: in-flight deployments (disabled text) first,
/// deployed rows (CheckMenuItems) below. Empty → single disabled placeholder.
fn build_models_submenu(
    app: &AppHandle,
    models: &[ModelInfo],
    deployments: &[DeploymentProgress],
) -> tauri::Result<Submenu<Wry>> {
    let serving_count = models.iter().filter(|m| m.is_serving).count();
    // Right-aligned trailing text isn't possible in native menu items, so fold
    // the serving summary into the parent label instead ("Models — 2 serving").
    let submenu_label = if serving_count > 0 {
        format!("Models - {serving_count} serving")
    } else {
        "Models".to_string()
    };

    if models.is_empty() && deployments.is_empty() {
        let placeholder = MenuItem::with_id(
            app,
            MENU_ID_MODELS_PLACEHOLDER,
            "(no models serving)",
            false,
            None::<&str>,
        )?;
        return Submenu::with_id_and_items(app, "models", &submenu_label, true, &[&placeholder]);
    }

    // MenuItem and CheckMenuItem are distinct types; own them separately
    // and collect refs of both into a single Vec<&dyn IsMenuItem>.
    let deploy_items: Vec<MenuItem<Wry>> = deployments
        .iter()
        .map(|d| {
            MenuItem::with_id(
                app,
                format!("deploy-{}", d.pipeline_id),
                deployment_label(d),
                false,
                None::<&str>,
            )
        })
        .collect::<tauri::Result<Vec<_>>>()?;

    // Native checkmark = serving state.
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

    let mut refs: Vec<&dyn IsMenuItem<Wry>> = deploy_items
        .iter()
        .map(|i| i as &dyn IsMenuItem<Wry>)
        .collect();
    refs.extend(items.iter().map(|i| i as &dyn IsMenuItem<Wry>));
    Submenu::with_id_and_items(app, "models", &submenu_label, true, &refs)
}

/// Label for a model row; port suffix disambiguates mixed-port llama-swap
/// instances. Serving state is shown via the leading icon on the IconMenuItem.
fn model_label(m: &ModelInfo) -> String {
    match m.port {
        Some(port) => format!("{} · :{}", m.alias, port),
        None => m.alias.clone(),
    }
}

/// Label for a deployment row: stage + integer percent + model file name.
fn deployment_label(d: &DeploymentProgress) -> String {
    let name = d.model_name.as_deref().unwrap_or(&d.pipeline_id);
    // "Queued foo.gguf" is complete on its own — the 0% tail adds no signal.
    if d.stage == "queued" {
        return format!("Queued {name}");
    }
    let verb = match d.stage.as_str() {
        "downloading" => "Downloading",
        "configuring" => "Configuring",
        // Surface the raw string for future stages so the row still says something.
        other => other,
    };
    format!("{verb} {name} — {}%", d.progress_pct as u32)
}

/// Digest of what the tray menu structure depends on. Progress is bucketed to
/// 5% steps to match the runtime's reporter cadence; alias/port renames don't rebuild.
fn menu_digest(
    models: &[ModelInfo],
    deployments: &[DeploymentProgress],
    update: Option<&str>,
) -> MenuDigest {
    let mut m: Vec<(String, bool)> = models
        .iter()
        .map(|m| (m.id.clone(), m.is_serving))
        .collect();
    m.sort();
    let mut d: Vec<(String, String, u32)> = deployments
        .iter()
        .map(|dep| {
            (
                dep.pipeline_id.clone(),
                dep.stage.clone(),
                (dep.progress_pct as u32) / 5,
            )
        })
        .collect();
    d.sort();
    MenuDigest {
        models: m,
        deployments: d,
        update: update.map(str::to_string),
    }
}

#[derive(Clone, PartialEq, Eq, Default)]
struct MenuDigest {
    models: Vec<(String, bool)>,
    deployments: Vec<(String, String, u32)>,
    update: Option<String>,
}

fn handle_menu_event(app: &AppHandle, event: tauri::menu::MenuEvent) {
    let id = event.id().as_ref();
    match id {
        MENU_ID_CONTROL => {
            if let Err(e) = app.opener().open_url(CONTROL_URL, None::<&str>) {
                eprintln!("[companion] failed to open {CONTROL_URL}: {e}");
            }
        }
        MENU_ID_PREFERENCES => {
            show_preferences_window(app);
        }
        MENU_ID_DOWNLOAD => {
            show_preferences_window(app);
            // Tell the window to open on the available-models section. Best-effort:
            // if it isn't listening yet (cold open), the emit is a harmless no-op
            // and the section is still reachable by scrolling.
            if let Err(e) = app.emit(EVENT_SHOW_DOWNLOADS, ()) {
                eprintln!("[companion] emit {EVENT_SHOW_DOWNLOADS} failed: {e}");
            }
        }
        MENU_ID_UPDATE => {
            // Claim the in-flight slot so a rapid double-click can't fire
            // overlapping /update POSTs (the second would hit a shutting-down
            // agent and pop a spurious failure dialog).
            let shared: SharedHandles = (*app.state::<SharedHandles>()).clone();
            {
                let mut guard = match shared.lock() {
                    Ok(g) => g,
                    Err(_) => return,
                };
                if guard.update_in_flight {
                    return;
                }
                guard.update_in_flight = true;
            }
            // Off the menu-event thread: the loopback POST shouldn't block the
            // tray. The next poll picks up the "Updating" state; poll_forever
            // releases the slot when the update finishes.
            let app_handle = app.clone();
            thread::spawn(move || {
                if let Err(e) = trigger_update(DEFAULT_UPDATE_URL) {
                    eprintln!("[companion] trigger_update failed: {e}");
                    // Failed before the agent went down — release so a retry works.
                    if let Ok(mut g) = shared.lock() {
                        g.update_in_flight = false;
                    }
                    // Otherwise the click looks like it did nothing — tell the user.
                    let dialog_handle = app_handle.clone();
                    let _ = app_handle.run_on_main_thread(move || {
                        dialog_handle
                            .dialog()
                            .message("Couldn't start the update. Check that Link is running, then try again.")
                            .title("Update failed")
                            .kind(MessageDialogKind::Error)
                            .show(|_| {});
                    });
                }
            });
        }
        MENU_ID_QUIT => {
            // Make the choice explicit: also stop the Link runtime, or just close
            // the tray (Link keeps running — the historical default). Non-blocking
            // so the tray/menu event loop isn't held; act in the callback.
            let app_handle = app.clone();
            app.dialog()
                .message(
                    "Also stop the Link service?\n\n\
                     \"Stop Link\" stops it and quits.\n\
                     \"Keep running\" just closes this app.",
                )
                .title("Quit Locai Link")
                .kind(MessageDialogKind::Info)
                .buttons(MessageDialogButtons::OkCancelCustom(
                    "Stop Link".to_string(),
                    "Keep running".to_string(),
                ))
                .show(move |stop| {
                    if stop {
                        if let Err(e) = preferences::runtime_stop() {
                            eprintln!("[companion] runtime_stop on quit failed: {e}");
                        }
                    }
                    app_handle.exit(0);
                });
        }
        _ if id.starts_with(MENU_ID_MODEL_PREFIX) => {
            let pipeline_id = id[MENU_ID_MODEL_PREFIX.len()..].to_string();
            handle_model_toggle(app, pipeline_id);
        }
        // Disabled items — shouldn't fire, ignore if they do.
        MENU_ID_STATUS | MENU_ID_MODELS_PLACEHOLDER => {}
        other => eprintln!("[companion] unhandled menu id: {other}"),
    }
}

/// Toggle a model's serving state. Runs the POST on a detached thread —
/// the Tauri menu-event thread is UI-adjacent and blocking on HTTP freezes the menu.
fn handle_model_toggle(app: &AppHandle, pipeline_id: String) {
    let handles_state: tauri::State<'_, SharedHandles> = app.state();
    let shared = (*handles_state).clone();

    // Lock once: read current state, claim the in-flight slot, flip is_serving
    // optimistically so an immediate second click reads the new state.
    let action = {
        let mut guard = match shared.lock() {
            Ok(g) => g,
            Err(e) => {
                eprintln!("[companion] handles mutex poisoned: {e}");
                return;
            }
        };
        if guard.in_flight.contains(&pipeline_id) {
            // A prior click is still in flight; drop this one.
            return;
        }
        let Some(model) = guard.models.iter_mut().find(|m| m.id == pipeline_id) else {
            eprintln!("[companion] click on unknown pipeline: {pipeline_id}");
            return;
        };
        let action = if model.is_serving {
            ServingAction::Stop
        } else {
            ServingAction::Start
        };
        model.is_serving = !model.is_serving;
        guard.in_flight.insert(pipeline_id.clone());
        action
    };

    thread::spawn(move || {
        let result = toggle_serving(DEFAULT_MODEL_ACTION_BASE, &pipeline_id, action);
        if let Err(e) = &result {
            eprintln!("[companion] toggle_serving({pipeline_id}, {action:?}) failed: {e}");
        }
        // Release the slot. On failure, revert the optimistic flip so the next
        // poll doesn't see a torn state; on success the poll reconciles anyway.
        if let Ok(mut guard) = shared.lock() {
            guard.in_flight.remove(&pipeline_id);
            if result.is_err() {
                if let Some(m) = guard.models.iter_mut().find(|m| m.id == pipeline_id) {
                    m.is_serving = !m.is_serving;
                }
            }
        }
    });
}

/// Poll `/healthz` and `/models` on a fixed cadence. Tray icon flips only on
/// Up↔Down transitions; the full menu is rebuilt only when the digest changes.
fn poll_forever(app: AppHandle, tray: TrayIcon, handles: Arc<Mutex<MenuHandles>>) {
    let mut current_tray = TrayState::Up;
    let mut current_digest = MenuDigest::default();
    // Track the mounted header text so an Up↔Down transition (which doesn't
    // touch the models digest) still forces a rebuild — IconMenuItem::set_text
    // on Tauri 2.11 didn't reliably repaint the disabled header row.
    let mut current_status_text = status_text_initial();
    // Update-in-progress inference: the agent's health server goes down during
    // the swap. If it was up with an update available and just dropped, treat
    // it as updating (clicking the tray Update item produces exactly this) and
    // hold that until it returns — success/failure both resolve on next Up.
    let mut updating = false;
    let mut update_saw_down = false;
    let mut prev_up_with_update = false;
    // Bound how long we display "Updating" while the agent is down, so a swap
    // that never comes back (crash, failed relaunch) falls back to OFFLINE
    // instead of showing "Updating" forever. ~5 min at a 5s poll.
    let mut update_down_polls = 0u32;
    const MAX_UPDATE_DOWN_POLLS: u32 = 60;

    loop {
        let health = agent_health(DEFAULT_HEALTH_URL);
        let models = list_models(DEFAULT_MODELS_URL);

        let (is_up, has_update) = match &health {
            HealthStatus::Up(h) => (true, h.update_available),
            HealthStatus::Down | HealthStatus::Malformed(_) => (false, false),
        };
        let was_updating = updating;
        if prev_up_with_update && !is_up {
            updating = true;
        }
        if updating && !is_up {
            update_saw_down = true;
            update_down_polls += 1;
            if update_down_polls >= MAX_UPDATE_DOWN_POLLS {
                updating = false;
                update_saw_down = false;
            }
        }
        if updating && update_saw_down && is_up {
            updating = false;
            update_saw_down = false;
        }
        if !updating {
            update_down_polls = 0;
        }
        // Release the click guard once an in-progress update resolves (agent
        // returned, or the timeout gave up) so the menu item works again.
        if was_updating && !updating {
            if let Ok(mut h) = handles.lock() {
                h.update_in_flight = false;
            }
        }
        prev_up_with_update = is_up && has_update;

        // Keep the icon on the Up glyph while updating so it doesn't flash the
        // offline icon during the swap.
        let next_tray = if updating {
            TrayState::Up
        } else {
            TrayState::from(&health)
        };
        if next_tray != current_tray {
            let bytes = match next_tray {
                TrayState::Up => TRAY_ICON_UP,
                TrayState::Down => TRAY_ICON_DOWN,
            };
            if let Ok(img) = Image::from_bytes(bytes) {
                // Atomic swap keeps the template flag in sync with the new image.
                if let Err(e) = tray.set_icon_with_as_template(Some(img), TRAY_ICON_IS_TEMPLATE) {
                    eprintln!("[companion] tray.set_icon failed: {e}");
                }
            }
            current_tray = next_tray;
        }

        let status_text = if updating {
            status_text_updating()
        } else {
            status_label(&health, &models)
        };

        // Tray tooltip mirrors serving state — Discord/Slack-style hover hint.
        let brand = format!("Locai Link{}", *CHANNEL_SUFFIX);
        let tooltip = match (&health, &models) {
            (HealthStatus::Up(_), ModelsStatus::Ok(list)) => {
                let n = list.iter().filter(|m| m.is_serving).count();
                match n {
                    0 => brand.clone(),
                    1 => format!("{brand} · Serving 1 model"),
                    n => format!("{brand} · Serving {n} models"),
                }
            }
            (HealthStatus::Up(_), _) => brand.clone(),
            _ => format!("{brand} · Offline"),
        };
        if let Err(e) = tray.set_tooltip(Some(&tooltip)) {
            eprintln!("[companion] tray.set_tooltip failed: {e}");
        }

        // Treat transient /models Down/Malformed as "keep last-known empty"
        // rather than clearing — avoids flapping when /models is briefly out
        // but /healthz still works. TODO: dedicated "stale" state.
        let new_models: Vec<ModelInfo> = match &models {
            ModelsStatus::Ok(list) => list.clone(),
            ModelsStatus::Down | ModelsStatus::Malformed(_) => Vec::new(),
        };
        let new_deployments: Vec<DeploymentProgress> = match &health {
            HealthStatus::Up(h) => h.deployments.clone(),
            HealthStatus::Down | HealthStatus::Malformed(_) => Vec::new(),
        };
        let update_latest: Option<String> = match &health {
            HealthStatus::Up(h) if h.update_available => h.latest_version.clone(),
            _ => None,
        };
        let new_digest = menu_digest(&new_models, &new_deployments, update_latest.as_deref());
        let text_changed = status_text != current_status_text;

        if new_digest != current_digest || text_changed {
            match build_tray_menu(
                &app,
                &new_models,
                &new_deployments,
                &status_text,
                update_latest.as_deref(),
            ) {
                Ok((new_menu, new_status_item)) => {
                    if let Err(e) = tray.set_menu(Some(new_menu)) {
                        eprintln!("[companion] tray.set_menu failed: {e}");
                    }
                    if let Ok(mut h) = handles.lock() {
                        h.status_item = new_status_item;
                        h.models = new_models.clone();
                    }
                    current_digest = new_digest;
                    current_status_text = status_text.clone();
                }
                Err(e) => eprintln!("[companion] build_tray_menu failed: {e}"),
            }
        } else if let Ok(mut h) = handles.lock() {
            h.models = new_models;
        }

        thread::sleep(POLL_INTERVAL);
    }
}

/// Binary status header. Version + serving count live in Preferences and on
/// the Models submenu label respectively — this row is a single "up or not" cue.
fn status_label(health: &HealthStatus, _models: &ModelsStatus) -> String {
    match health {
        HealthStatus::Up(_) => status_text_up(),
        HealthStatus::Down | HealthStatus::Malformed(_) => status_text_down(),
    }
}
