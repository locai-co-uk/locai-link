# Changelog

<!-- Each version entry leads with a short bullet summary. Those bullets are
published verbatim as the GitHub release notes by .github/workflows/release.yml,
so write them for the reader of the release. Keep pending work under
[Unreleased] and rename it to the version on release. Detail goes in the ###
sections below the summary. -->

## [Unreleased]

- Open a Workspace: the menu-bar companion gains an "Open a Workspace" menu item
  that opens Workspace (workspace.locai.co.uk) in the browser.
- The macOS installer now advertises Apple silicon only, matching what actually
  ships, so an Intel Mac is no longer offered a build it could never update.
- Uninstall now fully removes the Setup Assistant's per-user data; its bundle
  identifier in the uninstaller was mismatched, leaving caches and preferences
  behind.
- The router/plugin provisioner now extracts downloaded archives through the same
  path-traversal-guarded extractor as the updater, so a malicious or corrupt
  mirror can no longer write outside the target directory.
- Launching the companion a second time (from the Dock, Launchpad, or the copy
  in /Applications) no longer starts a duplicate menu-bar icon; it focuses the
  running app instead.
- The companion Preferences window now shows the installed version on every
  install layout (it previously showed nothing when the active version was
  tracked by a text pointer rather than a symlink).
- Hardening: the service manager no longer runs commands through a shell, and
  the source-install service label now uses the same reverse-DNS namespace as
  the packaged app.
- Remove models from within the companion: each deployed model gains a "Remove"
  action that deletes it locally and updates the dashboard so it no longer shows
  a model the device no longer has.
- Uninstalling now deregisters the device from Control, so the dashboard no
  longer keeps a stale offline row after uninstall.

### Added: "Open a Workspace" companion action (`crates/companion/src-tauri/src/lib.rs`)

- The tray gains an "Open a Workspace" item that opens Workspace
  (workspace.locai.co.uk) in the default browser.

### Fixed: duplicate companion instance on second launch (`crates/companion/src-tauri/src/lib.rs`)

- The companion had no single-instance guard, so launching it again (Dock,
  Launchpad, or the pkg-managed /Applications copy that OTA leaves at the prior
  version) started a second process and a duplicate tray icon. It now registers
  tauri-plugin-single-instance as its first plugin; a second launch focuses the
  running app's Preferences window and exits without adding a tray icon.

## [1.1.2]

- macOS whole-app OTA now updates the menu-bar companion, not just the runtime.
  The companion's LaunchAgent pointed at a binary name that never shipped, so
  launchd could never run or relaunch it; correcting the path fixes the OTA
  relaunch, first launch, and relaunch at login.
- Devices installed before this fix repair themselves over OTA: on startup the
  runtime detects a companion LaunchAgent with the wrong path, rewrites it, and
  relaunches the tray, so no reinstall is needed. A companion that still cannot
  refresh prompts a reinstall as a fallback.
- The Setup Assistant and the OTA relaunch no longer leave two menu-bar icons.
- Checkboxes in the Setup Assistant and companion render consistently across
  platforms and themes, and hitting the device limit during setup shows a clear
  next step instead of a raw backend error.
- End-to-end update coverage (install, update, reinstall, uninstall) runs in CI
  on macOS, so update regressions are caught before release.

### Fixed: companion LaunchAgent binary path, the OTA root cause (`bundling/pkg/LaunchAgents/uk.co.locai.link.companion.plist`)

- The plist `ProgramArguments` pointed at `.../Contents/MacOS/Locai Link`, but the
  Tauri build produces `.../Contents/MacOS/locai-link-companion`. That path never
  existed, so `launchctl` could not launch the companion (EX_CONFIG) and it only
  came up via an `open -a` fallback. Correcting the path lets launchd run the
  install-root copy the OTA swaps, so `kickstart -k` after a swap relaunches it.

### Added: self-heal for stale companion LaunchAgents (`src/link/app/updater.py`)

- On startup the runtime detects a companion LaunchAgent whose program path is
  wrong (installs from before the fix), rewrites it in place, drops any stale
  open-a instance, and re-bootstraps the launchd copy. This repairs existing
  installs over OTA, since the OTA payload does not carry the plist. The
  drift-detection reinstall prompt remains as a fallback.

### Fixed: no duplicate companion on relaunch (`crates/setup_assistant/src-tauri/src/lib.rs`, `src/link/app/updater.py`)

- The Setup Assistant falls back to `open -a` only when `kickstart` fails, so it
  no longer starts a second tray once the plist is correct.
- The OTA recovery relaunch bootstraps then kickstarts without `-k`, so it does
  not race a second companion instance.

### Fixed: macOS whole-app OTA relaunch hardening (`src/link/app/updater.py`, `crates/companion/`)

- After swapping a UI `.app`, strip `com.apple.quarantine` and re-verify its code
  signature; a quarantined bundle silently blocks a launchctl-driven relaunch.
- The companion relaunch kickstarts in place, and if the service is not reachable
  rebootstraps from the installed plist and retries, then falls back to
  LaunchServices.
- Drift detection compares the running companion version (published by the
  companion at launch), not the on-disk bundle, so a swap that landed but did not
  relaunch is caught.

### Fixed: cross-platform checkboxes + device-limit error (`crates/setup_assistant/src/App.svelte`, `crates/companion/src/App.svelte`)

- Replaced native `accent-color` checkboxes with a fully custom `appearance:none`
  control so they render identically on webkit2gtk and WKWebView, in light and
  dark, instead of being invisible in dark mode on macOS.
- Registration surfaces a friendly "maximum number of registered devices" message
  with a remediation step, and a generic fallback for other errors, so raw
  backend text is never shown to the user.

### Added: E2E update tests (`.github/workflows/e2e.yml`, `tests/`)

- `macos-ota-e2e` asserts the on-disk app version actually changes after an OTA
  (real `ditto` swap), gating every PR to main.
- `macos-lifecycle` runs the real `postinstall` as root and asserts the
  root-to-user ownership handoff the swap depends on, then reinstall-over-top
  preserves models and session data.
- Cross-artifact + hash-gating unit tests guard that the LaunchAgent, updater,
  and postinstall agree on where the app lives, and that a version bump changes
  the app hash so the swap actually fires.

## [1.1.1]

- macOS whole-app OTA reliably swaps the desktop apps: the update relaunches
  the install-root companion copy the LaunchAgent actually starts.
- A stale UI after an incomplete update is detected and prompts a reinstall, so
  a device can't silently sit on a new runtime behind an old UI.

### Fixed — macOS whole-app OTA (`src/link/app/updater.py`, `bundling/pkg/`)

- The companion LaunchAgent and the OTA now target the user-owned install-root
  copy (`/Library/Locai/Locai Link.app`), not the admin-owned `/Applications`
  copy the user-context updater can't rewrite.
- The post-update companion relaunch is fire-and-forget, so a slow
  `launchctl kickstart` can't hang the update.
- Added a one-time drift check + reinstall prompt when the UI apps can't be
  swapped over OTA.

## [1.1.0]

- Whole-app OTA: updates refresh the menu-bar companion and Setup Assistant
  alongside the runtime, and macOS gets a working OTA path for the first time.
- The companion can pull new models on demand, without rerunning the Setup
  Assistant.

### Added — Whole-app OTA (`src/link/app/updater.py`)

- Updates now swap the companion and Setup Assistant apps too, not just
  the Python runtime. The bundle manifest records a content hash per app;
  an update re-installs only the apps whose hash changed and restarts the
  companion so the new build takes effect immediately.
- macOS OTA: releases publish a per-platform tarball carrying the runtime
  payload plus the notarised, stapled `.app` bundles, so a swapped-in app
  stays Gatekeeper-clean. macOS previously shipped only the `.pkg`, so
  devices had no update asset to fetch.
- The version check runs through Control's cached endpoint (fleet-safe);
  the download resolves the per-platform asset from the release.

### Added — In-app model downloads (`crates/companion/`)

- The companion requests and downloads additional models directly from
  its Available-models list, with live progress, without rerunning the
  Setup Assistant.

### Changed — Setup Assistant model selection

- The model list highlights a recommended default and filters to the
  models this device can actually serve.

## [1.0.19]

First end-to-end GUI install path for macOS + Linux: a `.pkg` (macOS)
or tarball (Linux) drops the frozen runtime, LaunchAgents / systemd
units, and two Tauri apps — a first-run **Setup Assistant** that signs
in, registers with Control, and deploys the models you pick, and a
menu-bar **companion** that runs after Finish showing agent health, the
deployed-models list with live download progress, and a Preferences
window. The scaffolding introduced in 1.0.17 (Cargo workspace, Tauri
crates, `/healthz`) is now wired end-to-end.

### Added — Setup Assistant (`crates/setup_assistant/`)

- Full onboarding wizard: splash detects an existing install and
  offers Continue / Re-register / Uninstall; new-install path walks
  through sign-in (OAuth device-code flow), device naming, model
  selection, and Finish. Runs unprivileged as the console user after
  the .pkg postinstall hands off with `sudo -u`.
- Model catalogue fetch from `GET /models/list_without_layers_info`
  (Control) filters to the signed-in user's models. Selected models
  are pre-registered via `mark_deployment_pending` so the companion's
  Models panel shows every row at 0 % from t=0 instead of one-at-a-time
  as the runtime processes deploys serially.
- Registration payload includes the installed `agent_version` (read
  from the installed manifest via `installed_version`), so Control no
  longer shows a device as "Version unknown" between register and the
  first lifecycle heartbeat.
- Finish step calls `wait_for_agent_ready` (polls `/healthz` for
  `transport.connected: true`, 15 s deadline + 400 ms settle) before
  dispatching Control `deploy_model` calls — otherwise the first
  DEPLOY_MODEL raced the agent's Zenoh subscriber setup and was
  silently dropped.
- Rejects empty / missing `access_token` in the OAuth poll response
  (was previously stored as `Some("")`, letting `require_token` hand
  out an empty bearer downstream).
- Design system: tokens split into `src/lib/tokens/tokens.{json,css}`;
  dark mode wired via `prefers-color-scheme` with per-token overrides;
  sign-in button uses the shared brand logo asset.

### Added — Menu-bar companion (`crates/companion/`)

- Tray icon polls `/healthz` + `/models` every 2 s. Menu structure:
  status header, Models submenu (in-flight downloads with % + deployed
  models as toggleable CheckMenuItems), separator, Open Control Plane,
  Preferences…, Quit. Dynamic tray tooltip mirrors serving state
  ("Locai Link · Serving N models" / "Locai Link" / "Locai Link · Offline").
- Preferences window (Svelte + `$state`): Device panel, Agent panel
  with Running/Stopped pill + uptime + version, Network panel with
  transport status, per-model rows with download progress wheel +
  Cancel button, Advanced (log file reveal + install root). Status pill
  gates on the first confirmed poll so a slow first `/healthz` doesn't
  flash "Stopped" on window open.
- Cancel-deploy plumbed end-to-end: Preferences Cancel button →
  Tauri command → shared crate helper `cancel_deployment` → runtime
  `POST /models/{pipeline_id}/cancel-deploy` → sets a `threading.Event`
  and closes the streaming response so `iter_content` unblocks
  immediately.
- Model-toggle in-flight guard: clicking a serve/stop row twice in
  quick succession no longer sends duplicate/conflicting requests —
  a `HashSet<pipeline_id>` blocks re-clicks until the HTTP call
  returns, and the optimistic local flip means the next click reads
  the new state.

### Changed — Runtime deploy hardening

Builds on the worker-thread + CANCEL_DEPLOY primitive shipped in 1.0.18:

- Cancel-deploy is now propagated to the streaming HTTP response —
  the worker stashes the `requests.Response` and `_cancel_deploy`
  closes the socket so `iter_content` unblocks in milliseconds instead
  of waiting for the read timeout.
- Truncated-download guard: after the chunk loop, if `total > 0 &&
  done != total` the partial is deleted and the deploy is reported as
  failed rather than atomically publishing a short file.
- Worker-level `except` block on `_deploy_worker` catches
  mkdir / rename / commit failures so terminal status is always
  emitted (was previously silent on late failures).
- Deploy-lock race fix: `thread.start()` now runs inside the same
  `with self.lock` block that registers the worker, so a
  `CANCEL_DEPLOY` arriving between register and start can no longer
  observe `thread.is_alive() == False` and short-circuit.
- Health server extensions: `POST /models/{pipeline_id}/{serve,stop-serving,cancel-deploy}`
  action endpoints; `/healthz` now carries `deployments[]` (in-flight
  progress) alongside `models[]` and `transport{}`. The `deployments`
  read + write paths take a lock so the handler thread can't race the
  worker threads on `dict.values()`.

### Added — macOS .pkg installer (`bundling/pkg/`)

- `postinstall`: creates `versions/`, `state/`, `logs/` under
  `/Library/Locai/`, chowns the runtime-writeable subtrees to the
  invoking user, installs a `/usr/local/bin/locai` CLI symlink,
  `ditto`s both `.app` bundles into `/Applications`, runs
  `lsregister -f` + `mdimport` so LaunchServices + Spotlight index them
  immediately, and hands off to the Setup Assistant as the console
  user.
- `uninstall.sh` (run as root; SA "Uninstall" button uses osascript
  admin prompt; also runnable via `sudo /Library/Locai/uninstall.sh`):
  bootstraps out both LaunchAgents, `pkill`s stragglers, `lsregister -u`
  on both `.app`s, `rm -rf` on `/Library/Locai/`, `/Applications/*.app`,
  `/usr/local/bin/locai`, wipes per-user Tauri caches under
  `~/Library/{Caches,WebKit,HTTPStorages,Preferences,Saved
  Application State,Application Support}/uk.co.locai.link.{companion,setup}/`,
  removes pinned Dock tiles from `com.apple.dock.plist` (best-effort),
  and forgets the pkg receipt.
- Guards against silent no-ops: hard `$EUID != 0` check on uninstall
  with an actionable message; `sed`-based version stamp of
  `Distribution.xml` verified with `grep -q` so a placeholder drift
  fails the build rather than shipping a `.pkg` labelled "0.0.0".
- LaunchAgent `KeepAlive` set to `{ SuccessfulExit = false }`: crash
  → launchd restarts; user ⌘Q → stays quit.

### Added — Linux tarball installer (`bundling/linux/`)

- `install.sh`: unpacks the bundle into `${LOCAI_INSTALL_ROOT:-$HOME/.local/share/locai}`,
  installs `systemd --user` unit files, drops `.desktop` entries under
  `$HOME/.local/share/applications/` (with `@HOME@` sentinel
  substitution so KDE + GNOME both resolve `Exec=`), copies hicolor
  icons at 32/128 px, and starts the agent + companion via `systemctl
  --user`.
- `uninstall.sh`: reverse — stop units, remove `.desktop` entries and
  hicolor icons, `rm -rf` the install root.
- `pack.sh`: local packing helper that assembles the release tarball
  from an already-frozen runtime layout.

### Added — Shared Rust crate (`crates/shared/`)

- `agent_health(url)` / `list_models(url)` / `toggle_serving(base, id, action)`
  / `cancel_deployment(base, id)` — thin blocking-HTTP helpers over
  the loopback health API, so SA + companion don't duplicate ureq
  wiring.
- `installed_version(&install_root)` reads `current -> versions/<v>/manifest.json`
  (symlink or text pointer) and returns the resolved path + version.
- `read_boot_json` with strict field-type validation on `boot.json`.
- `autostart` module (macOS + Linux) exposing user-scope enable /
  disable helpers via `launchctl` and `systemctl --user`.
- `DeploymentProgress` + `ModelInfo` + `TransportHealth` — shared
  serde types with matching TypeScript definitions on both Tauri
  fronts.

### Changed — Release artifacts (`.github/workflows/release.yml`)

- **macOS**: publishes `.pkg` + `.pkg.sha256` only. The runtime-only
  `.tar.gz` previously produced by the macOS leg is dropped — the
  `.pkg` is the intended install path and the tarball was redundant.
- **Linux**: `.tar.gz` + `.tar.gz.sha256` is now the full
  `bundling/linux/pack.sh` layout — frozen runtime under `bundle/`,
  Setup Assistant + companion Tauri binaries, `boot.json`, systemd
  units, `.desktop` entries, hicolor icons, `install.sh`,
  `uninstall.sh`. Was previously just `dist/locai-link/` (the frozen
  runtime by itself). CI builds the Tauri binaries with
  `npm run tauri build -- --no-bundle` on `ubuntu-latest` after
  installing webkit2gtk-4.1 + gtk3 + libsoup deps.
- **Windows**: unchanged — `.zip` + `.zip.sha256` of the frozen
  runtime.
- Source archives (`.zip` + `.tar.gz`) are attached automatically by
  GitHub's Releases as before.

### Added — CI — Installers workflow (`.github/workflows/installers.yml`)

- Standalone workflow gated by `paths: [bundling/**, .github/workflows/installers.yml]`
  so unrelated PRs don't burn macOS-runner minutes. Concurrency
  group cancels overlapping runs on the same ref.
- Three jobs: `shellcheck` (all shipped shell scripts),
  `macos-roundtrip` (synthesises the .pkg install layout including
  per-user Tauri data, runs `uninstall.sh` as non-root — must fail
  with system paths intact — then as root, verifies every artefact is
  gone), `linux-roundtrip` (stages a synthetic tarball, runs
  `install.sh` against a scratch root, asserts systemd units +
  `.desktop` entries + icons + `@HOME@` substitution, then
  `uninstall.sh` + assert-gone).
- Would have caught (retroactively): the `uninstall.tool` typo, the
  silent-EACCES exit-0 non-root uninstall, missing user-data wipe,
  and `@HOME@` substitution failures.

### Changed — Companion Preferences performance

- `get_prefs_state` no longer probes `/healthz` (was redundant with
  `poll_status`); it now returns static/on-disk fields only. Cold-start
  cost drops to a single localhost RTT.
- `get_prefs_state` + `poll_status` are `async` on Tauri's
  `spawn_blocking` pool so slow HTTP responses don't stall the
  WebView main thread.
- Poll interval tightened from 4 s to 2 s.

### Fixed

- `resolve_agent_version`: docstring now lists the PyInstaller-frozen
  `manifest.json` lookup step (the code path already existed; docs
  were stale).
- Dependency and CI hygiene: dependabot ignore list for the GTK3-rs
  family (blocked upstream on wry publishing a webkitgtk6/GTK4
  backend); `cargo update` refreshes tauri 2.11.5, plist 1.10, quick-xml
  0.41, time 0.3.53, zbus 5.17.

## [1.0.18] - 2026-07-07

Companion menu-bar tray beta + runtime deploy pipeline groundwork.
First working end-to-end path: the runtime can accept a DEPLOY_MODEL
command from Control on a background worker thread, and a macOS
tray-icon app talks to it via `/healthz` + `/models` to display which
models are running. Everything else in the SA + GUI installer stack
builds on top of this.

### Added — Menu-bar companion (initial beta)

- New `crates/companion/` Tauri crate producing a tray-only macOS app.
  Icon in the menu bar reflects agent up/down; dropdown shows a status
  header, a Models submenu with per-pipeline serve/stop toggles, and a
  Quit item that stops both the runtime and the companion cleanly.
- Login-autostart wiring: the companion's `set_run_at_login` Tauri
  command flips `RunAtLoad` on the user's LaunchAgent plist.

### Added — Runtime deploy orchestration

- `DEPLOY_MODEL` now runs on a per-pipeline worker thread rather than
  blocking the command dispatcher, so parallel model deploys are
  supported.
- `CANCEL_DEPLOY` command signals the worker's cancel event and lets
  the download loop exit cleanly.
- Existing worker registry / status reporting extended with
  progress + cancel primitives so downstream telemetry sees a coherent
  in-flight → completed / cancelled / failed timeline.

### Changed — Tauri frontends

- `crates/setup_assistant/` and `crates/companion/`: swapped from
  SvelteKit onto bare Svelte + Vite. Both Tauri surfaces are single-page
  windows with no server, no routing, and no adapter — SvelteKit's
  machinery was dead weight. Standard Vite shape now: `index.html` at
  root, `src/main.ts` mounts `App.svelte` into `#app`, design tokens
  imported once in `main.ts`. Output moved from `build/` to `dist/`
  (Vite default); `tauri.conf.json.frontendDist` follows.
- Companion Vite dev server runs on port `1421` (setup_assistant on
  `1420`) so both apps can be run concurrently during dev; companion
  HMR moved to `1423` to sidestep setup_assistant's HMR port.

### Removed — TUI

- `src/link/ui/` deleted, along with the `tui` subcommand, the
  `--tui` setup flag, and the `textual>=7.0.0` optional dependency.
  The GUI installer (setup assistant + menu-bar companion) supersedes
  the Textual-based agent-management interface, which never made it
  past a niche developer utility.

### Fixed — Editor-only diagnostics

- `pyproject.toml`: added `[tool.pyright]` with `extraPaths = ["src",
  "bundling"]` so Pylance can resolve `from link.xxx import ...` under
  the src/ layout (mirrors the existing pytest `pythonpath`).
- `src/link/infra/service.py`: `ServiceManager` factory now passes
  `scope` / `label_prefix` as explicit keyword arguments to each
  backend rather than via a `**kwargs` dict, so type checkers keep the
  `ServiceScope` `Literal` instead of widening it to `str`.

## [1.0.17] - 2026-07-01

Lays down the over-the-air update path for bundled installs, adds a
lightweight `/healthz` endpoint for host-app integrators, and scaffolds
the Cargo workspace + Tauri surfaces the coming GUI installer will
build on. Developer (source) installs are unaffected by the OTA
machinery — they still update by `git pull` as before.

### Why a separate launcher?

Updating a running program while it's still running is the classic
"changing the wheels on a moving car" problem. On every supported
platform, a frozen Python process keeps its own executable and bundled
libraries open for as long as it's alive — you can't safely overwrite
those files in place, and the process can't replace itself. So the
agent fundamentally can't update itself on its own.

The launcher solves this by sitting one level up. It's a tiny stable
binary whose job is to pick which version of the agent to run, exec it,
and watch its exit code. When the agent wants to update, it just exits
with a known code (42) and the launcher re-resolves `current` — which
the update may have flipped to a new version — and starts that one
instead. The launcher itself is intentionally never auto-updated, which
makes it the **unchanging entry point** the host app (Meetily, SafeChat,
systemd, launchd, Windows SCM) talks to. Agent versions can come and go
behind a fixed `locai-link` binary name and a fixed argv contract,
without any host integration noticing.

The separation also makes **automatic rollback possible**. The same
outside-the-bundle process that started the new version can watch it
crash, decide the crash happened too soon after an update, and respawn
the previous version instead. The agent can't roll itself back — by
the time it's the one failing, it's no longer a credible judge of
itself.

And it's what makes **Pattern B first-launch** work at all. A host that
ships only the launcher + `boot.json` has nothing else to run on first
launch; the launcher has to be able to fetch and lay down the first
bundle itself before anything inside the bundle exists.

### What changes for users and operators

- **Bundled installs update themselves.** When the backend sends the
  existing `UpdateAgentCommand`, the agent now notices it's running as a
  frozen bundle and fetches the latest matching release from GitHub
  instead of trying (and failing) to `git pull`. Download is verified by
  SHA256 against a sidecar file published alongside the tarball before
  anything is extracted to disk.
- **Two versions are kept on disk at any time.** The current one and the
  one before it. After a successful update the older-still version is
  garbage-collected on the next round, so disk usage stays bounded at
  ~2× a bundle.
- **Automatic rollback if a new version crashes.** A small launcher
  binary sits in front of the runtime and watches its exit code. If a
  freshly-installed version exits non-zero within 2 minutes of the
  swap, the launcher points "current" back at the previous version and
  respawns. Past that window, crashes are treated as ordinary crashes.
- **Pre-swap health check.** Before the new version is made live, the
  runtime is started once with `--self-check` — it boots config + Zenoh
  + plugins and exits clean. If self-check fails the new version is
  discarded; the running one keeps serving.
- **No change for source installs.** If you're running Link from a git
  checkout, nothing in this release affects you. The frozen-vs-source
  dispatch picks the old path automatically.
- **First launch fetches its own bundle.** Partner hosts (Meetily,
  SafeChat) can ship just the launcher + a tiny `boot.json` describing
  what to download. On first run, the launcher reads `boot.json`,
  fetches the latest matching release off GitHub, verifies it the same
  way OTA does, and starts the agent. No bundle baked into the host
  installer required — and users always get the *latest* version on
  first run regardless of how long ago the host shipped. Hosts that
  prefer to pre-seed an offline-capable bundle can still do that; the
  launcher branches automatically based on whether a `current` already
  exists.

### Under the hood

- **A/B install layout.** Bundled installs now live at
  `<install_root>/versions/<v>/` with a `current` symlink (or `CURRENT`
  pointer file on hosts where symlinks aren't permitted) selecting the
  active version. `previous` keeps the immediately prior version for
  rollback.
- **Rust launcher binary (`locai-link`).** ~250 lines, ships in every
  release tarball. Resolves `current`, execs the runtime, restarts on
  exit code 42 (the runtime's "I updated myself, please respawn"
  signal), and performs the post-update rollback described above. The
  launcher itself is intentionally never auto-updated.
- **`updater.py` covers both paths.** `pull_and_update` /
  `reinstall_plugin_binaries` for source installs is unchanged.
  `swap_bundle()` is new: identify-self via `manifest.json`, query the
  GitHub Releases API for the matching asset, download with retries,
  verify, extract to a staging path, health-check, atomic-rename
  `current`, drop a `.update-pending` stamp the launcher reads on next
  boot, GC the previous-previous version. `running_frozen_bundle()`
  routes `_apply_update_and_reexec` to the right one.
- **`.update-pending` stamp.** Two-line plain-text file
  (`<unix_ts>\n<previous_version>\n`) written immediately after a
  successful flip. The launcher reads it on every non-zero child exit
  and, within the 120s health window, rolls back. Stale stamps are
  cleared on next non-zero exit so they don't linger.
- **Self-check entry point.** `python -m link self-check` /
  `locai-link-runtime --self-check` boots config + transport + plugins
  and exits 0 on success. No telemetry, no side effects — explicitly
  designed to be safe to run on an unproven version.
- **Integration tests.** 52 Python tests across the source and bundle
  paths; 11 Rust integration tests in the launcher covering exec
  dispatch, exit 42 respawn, and all five rollback conditions
  (within-window, past-window, exit 0, previous-version missing,
  pointer-file shape).

### Added — `/healthz` endpoint

- `src/link/infra/health_server.py` — small loopback HTTP server on
  `127.0.0.1:8101` returning `{version, uptime_seconds,
  currently_serving, model_id}`. Separate from `ServingProxy` so it
  answers "is the agent alive" even when no model is loaded. Polled by
  the menu-bar companion app; also available to host-app integrators
  (SafeChat, Meetily) that want a lightweight state probe. Owned by
  `AgentRuntime`, mutated by the `StartServingCommand` /
  `StopServingCommand` handlers and the resume branch. Started lazily
  in `run()`, stopped in `_shutdown()`; port-in-use failures degrade
  gracefully to a warning.

### Added — Cargo workspace + Tauri scaffolding

- `crates/` top-level directory now hosts every native binary:
  `launcher/` (moved from repo root, history preserved),
  `shared/` (new helper crate with stubs for agent-status polling,
  `boot.json` reading, version lookup), and two Tauri app scaffolds
  (`setup_assistant/` and `companion/`) that the GUI installer flow
  will grow into. Inert in this release — nothing on the shipping
  agent path calls them, and CI path filters skip Tauri-only changes.
- `bundling/build.py`, `.github/workflows/bundle.yml`, and
  `.github/workflows/release.yml` updated to point at
  `crates/launcher/` and the workspace target dir.
- `bundling/pkg/boot.json` — production launcher config (channel,
  asset repo, plugin set) shipped by the coming `.pkg` postinstall.

### Added — Launcher configurability

- `MacOSBackend` (`src/link/infra/service.py`) gains a
  `scope: "user" | "system"` parameter — `system` writes the plist to
  `/Library/LaunchAgents/` (the GUI installer's "install for all
  users" path); `user` keeps the historical `~/Library/LaunchAgents/`
  behaviour that existing `main.py deploy` callers get by default.
- `label_prefix` parameter replaces the hard-coded `io.locai.{name}`
  so the GUI installer can label plists as `uk.co.locai.link.agent` /
  `uk.co.locai.link.menubar` to match its bundle identifier. Existing
  callers keep `io.locai` — no behaviour change without opt-in.
- `install_all(services, start_now)` helper registers multiple
  LaunchAgents in lockstep with rollback on partial failure. The Setup
  Assistant's Finish step will call this so one "Run at login" toggle
  drives both the agent and menu-bar app.
- `launchctl list` grep tightened to anchor on the full label — no
  more false matches when two services share a prefix.

### Added — Onboarding browser handoff

- Device-flow SSO now opens the verification URL in the system
  browser (`open` / `xdg-open` / `start`) when running detached from a
  terminal (`sys.stdin.isatty() is False`). The existing stderr banner
  still prints — the browser call is an additive supplement, not a
  replacement. Interactive terminals see no behaviour change.

### Added — `.pkg` installer source skeleton

- `bundling/pkg/` now holds the productbuild sources for the coming
  macOS GUI installer: `Distribution.xml` (system-wide install to
  `/Library/Locai`, macOS 14 minimum, arm64+x86_64),
  `welcome.html` / `license.html` / `conclusion.html` for the wizard
  panes, and `scripts/postinstall` (chown, `/usr/local/bin/locai`
  symlink, launches Setup Assistant.app as the console user).
- `bundling/pkg/README.md` documents the pkgbuild + productbuild build
  sequence for the future release CI.
- Not built by any workflow yet — waiting on the Developer ID
  Installer certificate. Lands as sources so subsequent PRs can wire
  the CI without also having to author the wizard structure.

### Added — Design assets in Tauri apps

- Brand icons (`.icns`, `.ico`, PNG sizes for macOS / Windows / Android
  / iOS) generated via `tauri icon` from the design hand-off's
  512×512 `app-icon.png`, replacing the create-tauri-app placeholders
  under `crates/setup_assistant/src-tauri/icons/` and
  `crates/companion/src-tauri/icons/`.
- Design tokens (`tokens.css`, `tokens.json`) and SVG icon set (10
  glyphs — check, chevron, cloud, disk, download, search, spinner,
  trash, wifi) copied to each app's `src/lib/tokens/` and
  `src/lib/icons/`. Not yet consumed by any Svelte component — landed
  ahead of the Setup Assistant / Companion UI work so those tasks
  start with the design system already in the tree.

### Under the hood — Pattern B first-install now works

- The launcher's `bootstrap_from_boot()` (`crates/launcher/src/`)
  reads `boot.json`, fetches the release asset off GitHub, verifies
  the SHA256 sidecar, extracts to `versions/<v>/`, writes `current`,
  and execs the runtime — all before any bundle exists on disk. Host
  installers that ship just the launcher + `boot.json` are now viable
  (Pattern B) alongside the pre-extracted-bundle path (Pattern A).

### Not in this release

- **Health-window heartbeat from the runtime.** Today the launcher
  decides rollback eligibility purely by wall-clock age of the stamp
  (≤ 120s). Hooking the runtime to clear the stamp itself, once it
  passes its first healthy ping, lands when the runtime grows a
  periodic backend ping.
- **macOS `.pkg` GUI installer + Setup Assistant + menu-bar
  companion.** Scaffolds, sources, and design assets are all in the
  tree (`crates/setup_assistant/`, `crates/companion/`, `bundling/pkg/`),
  but nothing is wired into a build pipeline yet and none of the Tauri
  UI is functional. Coming in the 1.1.x line once the Apple Developer
  ID Installer certificate is provisioned and the wizard + menu-bar
  apps are built out.
- **Windows.** Code paths are in place but unsigned bundles are
  blocked by Windows SmartScreen — code-signing procurement gates
  Windows GA. macOS + Linux are not blocked.
- **Bundled update telemetry to backend.** `bootstrap_*` and
  `update_*` events are specified in the design doc but not yet
  emitted; they ride the existing event publisher when wired up.

## [1.0.16] - 2026-06-25

Restores serve-state reporting after an unclean shutdown, tightens command
input validation and the request-path security perimeter, hardens
`llama-swap` reclaim, and bumps `llama.cpp` to `b9789`.

### Added — Resume status reporting

- `AgentRuntime.run()` now re-announces model state to Control after
  recovering active pipelines from the session file. Previously a
  serving (or inference) model auto-resumed on restart would never tell
  Control it was back up, leaving the UI stuck on "not serving" until
  the user issued a fresh command. The recovery branch now emits the
  same `report_model(serving=True, …)` / `report_model(running=True, …)`
  that the `StartServingCommand` / `StartModelInferenceCommand`
  handlers emit, gated by `source.args["mode"] == "serve"` and
  `source.args["model_path"]` so telemetry/poller pipelines stay
  silent. Three new tests in `tests/test_runtime.py` pin the happy
  path, the negative case, and the failed-resume safety case.

### Changed — Plugin binaries

- `LLAMA_CPP_RELEASE`: `b9222` → `b9789`. Windows CUDA-13 prebuilt tag
  follows the upstream rename to `cuda-13.3` (was `cuda-13.1`);
  `cuda-12.4` branch unchanged. macOS/Linux URL patterns unchanged.

### Fixed — Security & input validation

- `UninstallModelCommand`: `filename_on_server` and `file_extension`
  validated as plain basenames before they flow into a filesystem
  delete. Same basename check on `model_name` in `DeployModelCommand`.
  Closes a path-traversal vector when malformed input would have
  composed the artifact path.
- `DeployModelCommand` / `UpdatePipelineCommand`: reject payloads whose
  `pipeline_id` doesn't match `config.id`. Previously the agent would
  silently persist under one key while restarts/lookups used the other.
- `ValidationError` detail no longer echoes through `report_command`.
  A generic "Invalid command payload" goes to telemetry; the full
  pydantic repr (which can include rejected field values) stays in
  local logs only.
- `serving_proxy.py`: new `_sanitize_header_value` helper used inside
  `_resolve_echo_origin` strips CR/LF from incoming `Origin` headers
  at the input boundary. The allowlist was already the load-bearing
  defense; this rules out HTTP response splitting by construction.
- `state.py`: `_tighten_permissions` runs **before** the
  version-equality check, so a session file with a stale schema
  doesn't keep loose perms while users resolve the schema drift.
- `infra/utils.py`: `_read_raw_id` rejects empty/whitespace results
  from native machine-id readers. An empty `/etc/machine-id` would
  otherwise hash identically across devices, collapsing enrolment
  dedup.

### Fixed — llama-swap reclaim

- `_looks_like_our_llama_swap` now also matches the cmdline against
  *this* manager's resolved config path or listen address. A stale
  pidfile whose PID had been reused by an unrelated llama-swap on a
  different port no longer gets killed. The test fixture was updated
  to use the resolved config path the production codepath actually
  passes to `Popen`.
- `SwapManager.is_healthy()` returns `False` when `_proc is None`,
  closing the hole where another process bound to the same loopback
  port could have tricked this manager into reporting healthy.
- New `SwapManager.remove_telemetry_callback()`; `LanguageModel.stop()`
  unregisters its callback before `remove_model`, plugging the
  fanout-to-dead-adapter leak.

### Fixed — Robustness

- `serving_proxy.py` SSE parser accepts both `\n\n` and `\r\n\r\n`
  event delimiters (the spec allows either; llama-server emits LF,
  other OpenAI-compatible servers emit CRLF).
- `ServingProxy.start()` raises on port-in-use instead of returning
  silently. Readiness probes against the upstream port would otherwise
  pass even with no public proxy actually listening.
- `LanguageModel` queue insertion uses `put_nowait` + `Full` rather
  than the racy `full()` → `put()` pair.
- `AgentCommand` dedup records `cmd.id` only after the callback
  succeeds. Failed callbacks no longer poison the dedup window;
  legitimate retries can proceed.

### Fixed — Reproducibility

- `bundling/manifest.py`: `plugins[]` in `manifest.json` is now sorted
  by the canonical `PLUGIN_ORDER` (matching the asset-name builder),
  so two CI runs that pass `--plugins` in different orders produce
  byte-identical manifests.
- `.github/workflows/release.yml`: workflow-level `contents: write`
  removed; per-job grants only (release-create + asset-attach).

### Changed — Docs

- `bundling/README.md`: stale `v1.0.14` examples replaced with
  `v<version>` placeholders.
- `plugins/language_model/README.md`: port-table row for `8100` now
  reads "configured host; default `127.0.0.1`" instead of the
  misleading "all interfaces".

## [1.0.15] - 2026-06-18

Cuts over inference observability to HTTP-response interception, ships the
typed command-wire contract, and refines the bundling + llama-swap
machinery introduced over 1.0.12–1.0.14.

### Added — Inference observability

- `src/link/infra/serving_proxy.py` — reverse proxy that always sits in
  front of `llama-swap`. Two independent features gated on construction
  args: ACAO/CORS headers (when `allowed_origins` is non-empty), and
  per-inference telemetry capture (when `on_telemetry` is set). Captures
  the OpenAI `usage` block from `/v1/chat/completions` responses
  (streaming SSE and non-streaming JSON), falls back to counting
  `delta.content` events when the client didn't request
  `stream_options.include_usage`. One telemetry record per request,
  fired on the callback.
- `LanguageModel._on_proxy_telemetry()` — bridges a ServingProxy
  inference record into the adapter's queue, sharing the same
  `ModelServer.build_telemetry_payload` shape as the legacy log-parse
  path. Filters records by `record["model"] == self.model_id` so two
  adapters sharing one SwapManager on the same port don't mis-attribute
  each other's inferences.
- `SwapManager.add_telemetry_callback()` + internal `_fanout_telemetry`
  — multi-adapter routing. Every `get_swap_manager()` call appends its
  callback so each served model gets its own telemetry stream; each
  adapter filters by model id internally.
- `tests/test_serving_proxy.py` — end-to-end tests against a real HTTP
  loopback: streaming + non-streaming token extraction, `usage` vs
  `delta_count` fallback, telemetry-off pass-through, all the CORS
  preflight / disconnect contracts the prior `cors_shim` test pinned.

### Added — Command contract

- `UninstallModelCommand` and `UpdatePipelineCommand` to the typed
  command schema (`src/link/config/commands.py`).
- `AgentRuntime._update_pipeline()` — applies an updated `PipelineConfig`
  in place, restarting the pipeline if running.
- `tests/test_command_wire_contract.py` + `tests/fixtures/wire/*.json` —
  golden-fixture round-trip tests for every command type. Mirrors the
  backend's `to_wire` contract.

### Added — llama-swap orphan handling

- `state/swap_<port>.pid` — every llama-swap process Link spawns writes
  its PID to this file. On next start, `_reclaim_previous_instance()`
  reads the pidfile, verifies via `psutil` cmdline that the PID is
  actually llama-swap (refuses to touch foreign processes that may have
  reused the PID), and terminates cleanly before binding the port. If
  the port is held by a non-llama-swap process, Link raises a
  diagnostic `RuntimeError` listing the platform-appropriate `lsof` /
  `netstat` command — no longer kills random processes.

### Added — Misc

- `src/link/utils/version.py` — agent-version resolver moved out of the
  logger module so it can be reused without pulling logging deps.
- `plugins/language_model/README.md` — new "Testing a served model"
  section spelling out the public port (8100, ServingProxy, telemetry
  fires) vs the internal port (8150, llama-swap directly, bypasses
  telemetry — useful only for triage).
- Backend integration guide at `docs/backend/` (CORS allowlist wiring,
  analytics architecture, OTA plan).
- `TODO.md` — OTA update path + Windows code-signing wiring (deferred).

### Changed — Serving proxy

- The CORS shim has been renamed and broadened into `ServingProxy`. The
  old name (`CorsProxy`, in `src/link/infra/cors_proxy.py`) is gone;
  the new file is `src/link/infra/serving_proxy.py`. CORS is now one of
  two optional features the proxy provides (the other being telemetry
  capture); both are independently configurable.
- `SwapManager` always fronts llama-swap with `ServingProxy`,
  regardless of CORS state. llama-swap binds the loopback-only listen
  port (`public_port + 50`); the proxy owns the public port.
  Previously the proxy was only instantiated when CORS was on.

### Changed — Command contract

- `handle_command` now validates every command against the typed
  `Command` contract via `parse_command` (after resolving
  `${identity.*}` placeholders) and dispatches on the typed object.
  The wire format is the flat `{id, type, ...}` shape; the old
  `command`/`command_type`/`payload` envelope is no longer accepted.
  Commands that fail validation are reported `failed` (when carrying
  an `id`) rather than silently dropped.
- `_deploy_model` stores the provided `config` (`PipelineConfig`)
  verbatim; the agent no longer derives a pipeline from
  `runtime_config`. The backend ships ready-made `PipelineConfig`
  definitions.
- `AgentCommand` dedup keys on the wire `id` field (was `command_id`).
- `runtime.py` `START_SERVING` handler: `alias = cmd.model_display_name`.
  llama-swap routes by the human-readable display name; the canonical
  `pipeline_id` (UUID) is used independently in the Zenoh sink topic
  (`locai/devices/<device_id>/models/<pipeline_id>/results`) and so
  carries through to Firestore + PostHog for backend attribution. The
  two id spaces are deliberately separate.

### Changed — Telemetry

- The language_model adapter no longer log-parses llama-swap stdout
  for inference timing; telemetry now flows through the ServingProxy's
  response interception. `ModelServer`'s log-parse path is preserved
  as the legacy fallback for the (rare) case where llama-swap isn't
  installed.

### Changed — Bundling

- `bundling/manifest.py` replaces the old `bundle_profile.py`
  YAML-driven profile machinery. The build CLI takes a flat
  `--plugins` list; the manifest is the authoritative record of
  what's in a bundle.

### Changed — Plugin install

- Plugin install scripts are quieter when binaries are already
  installed at the pinned tag — early-return without log noise.

### Removed — Serving proxy

- `src/link/infra/cors_proxy.py` (renamed to `serving_proxy.py`;
  imports updated across the repo).

### Removed — Bundling

- `bundling/bundle_profile.py` and the YAML profile artefacts. The
  build CLI's `--plugins` flag is the canonical input.
- The `--profile` flag from `bundling/build.py` and any references to
  partner-named bundles.

### Removed — Command contract

- `AgentRuntime._normalise_command` and `_map_runtime_to_pipeline_config`
  — the backend now sends ready-made pipelines, the agent does no
  derivation.
- The legacy `REMOVE_MODEL` command alias. Use `UNINSTALL_MODEL`.

### Removed — Release plumbing

- The `RELEASE_TOKEN` PAT requirement — the consolidated release
  workflow uses the default `GITHUB_TOKEN`.

### Fixed — llama-swap

- Orphans surviving an unclean Link shutdown — `_start()` now reliably
  reclaims its own previous instance via pidfile and refuses to touch
  anything else.

### Fixed — Telemetry

- Inference telemetry going dark when serving uses llama-swap (i.e.
  production). The legacy log-parse hook in `LanguageModel` was wired
  only to the `ModelServer` fallback path; in swap mode no telemetry
  was ever emitted. ServingProxy + `_on_proxy_telemetry` close this
  gap end to end.
- Multi-model attribution: when two models served on the same port
  shared one `SwapManager`, the second adapter's telemetry callback
  was silently dropped and all chats attributed to the first model.
  The fanout + per-adapter filter fixes this.
- `_ChatTelemetry._absorb` no longer overwrites the captured model id
  from the response body — the server echoes the gguf file stem,
  which is not the canonical id the request used. Request body is the
  authoritative source for attribution.

## [1.0.14] - 2026-06-04

Finalises the macOS bundle to a signed, notarised, drag-to-extract DMG;
adds the `UNINSTALL_MODEL` command handler; ships the Windows one-liner
installer.

### Added — Bundling polish

- Drag-to-extract DMG packaging for macOS, replacing the earlier zipped
  layout. Verified end-to-end on Apple Silicon.
- Developer ID Application signing of every binary in the bundle, with
  hardened-runtime entitlements and Apple-notary submission via
  `notarytool`. `spctl` now validates the main executable, not just the
  app shell.
- `.github/workflows/release-assets.yml` and `bundling/sign_macos.py`
  write signing diagnostics to stderr so notary failures don't get
  hidden in stdout-only logs.

### Added — Commands

- `UNINSTALL_MODEL` command handler (#329). The agent now deletes
  installed model artefacts on command, freeing disk without requiring
  a manual file removal on the device.

### Added — Installers

- Windows `install.cmd` one-liner — previous Windows onboarding path
  required PowerShell only.

### Fixed — Local Zenoh

- Local zenoh setup no longer fails when the router config path is
  resolved relative to an unexpected cwd. Trivial guard, but it was
  blocking single-device dev loops.

## [1.0.13] - 2026-05-29

Lands the `audio_transcriber` plugin (whisper.cpp) with a working macOS
prebuilt path.

### Added — Plugins

- `plugins/audio_transcriber/` — new plugin packaging `whisper.cpp` for
  on-device speech-to-text. Install script downloads macOS arm64/x64
  prebuilts; Linux and Windows still build from source.
- macOS build path for `audio_transcriber` mirrors the
  `language_model` plugin's prebuilt-download pattern.

## [1.0.12] - 2026-05-28

First cut of the bundling subsystem and the `SwapManager`-backed
language-model plugin — the foundation the 1.0.14 signing work and the
1.0.15 ServingProxy build on.

### Added — Bundling

- `bundling/` directory with the initial PyInstaller spec
  (`bundling/locai-link.spec`), a build CLI (`bundling/build.py`), and
  a prefetch step that resolves plugin binaries (`llama.cpp`,
  `whisper.cpp`) before the binary is assembled.
- `.github/workflows/bundle.yml` — runs the bundle on every PR that
  touches bundling, plugin install scripts, or runtime code, matrixed
  across Ubuntu / Windows / macOS.

### Added — llama-swap

- `SwapManager` rewrite (~160 lines) — manages a single `llama-swap`
  process per port, hot-swapping models on demand instead of spawning
  one `llama-server` per pipeline. Foundation for ServingProxy
  telemetry in 1.0.15.

### Fixed — Onboarding

- Registration metadata now includes `agent_version`, resolved the
  same way `report_lifecycle` does (`_AGENT_VERSION` with a
  `pyproject.toml` fallback). Closes the null-on-creation window where
  new devices appeared in the backend with no version string.

### Fixed — Runtime

- Generic model error path in `runtime.py` no longer swallows the
  exception. `_start_pipeline` failures now produce a structured
  `report_command(..., "failed", ...)` instead of dropping silently.

### Fixed — Bundling

- Plugin server scripts (`plugins/*/server.py`) tightened so PyInstaller
  picks them up on Windows and macOS — was bundling fine on Linux but
  not on the other two.

## [1.0.11] - 2026-05-18

Patch — restores the `agent_version` field in lifecycle reports under
PyInstaller-frozen builds.

### Fixed — Reporting

- `LinkReporter.report_lifecycle()` falls back to
  `_resolve_agent_version()` at report time when `_AGENT_VERSION` was
  empty at module-import time. PyInstaller-frozen environments can
  defer the resolver, which left lifecycle messages arriving with
  `agent_version: null` on bundled builds and broke version-keyed
  dashboards.

## [1.0.10] - 2026-05-18

Adds the OAuth device flow for SSO-only accounts and surfaces
previously-silenced onboarding errors.

### Added — Onboarding

- OAuth device flow fallback for SSO users. When `/auth/login` returns
  HTTP 409 with `use_device_flow` in the body, the CLI now opens a
  `control.locai.co.uk/link?user_code=…` URL and polls for approval —
  no password needed for accounts that authenticate via SSO.

### Fixed — Onboarding

- Empty password (the natural reflex for SSO accounts) short-circuits
  straight to the device flow rather than POSTing `password=""` to
  `/auth/login`. The backend's form parser rejected the empty string
  with HTTP 422 before the 409 `use_device_flow` signal could fire, so
  the CLI exited with a validation error that gave no clue the user
  was on an SSO account. Prompt now reads "Enter platform password
  (leave blank for SSO accounts)".
- `main.py:run()` no longer swallows the resulting `RuntimeError`
  silently. Logs the exception with `exc_info=True` before exiting so
  future onboarding failures produce a visible traceback instead of
  disappearing after "Authenticating with the platform...".

## [1.0.9] - 2026-05-08

Adds deployment progress reporting, command dedup, Zenoh TLS/auth
support, cross-platform temperature sensing, and end-to-end installer
tests; cleans up the Windows dependency chain.

### Added — Reporting & dedup

- `LinkReporter.report_deployment_progress()` — incremental model
  deployment events (`downloading`, `configuring`, `completed`) with
  byte counts; throttled to 5% steps.
- `AgentCommand._seen` deque + `mark_seen()` — bounded `command_id`
  dedup to support online-reconcile flows where Firestore HTTP backlog
  and live Zenoh inbox samples overlap.

### Added — Zenoh client

- `tls_root_ca`, `username`, `password` args in `transport.args`.
  `tls_root_ca: "auto"` resolves to the `certifi` bundle at runtime —
  no PEM file needs to live on disk.

### Added — Installers

- One-liner installers (`install.sh`, `install.cmd`) honor `--branch`
  and `--repo-url` CLI args, matching `install.ps1`. Env vars
  (`LOCAI_BRANCH`, `LOCAI_REPO_URL`) still respected; CLI overrides
  them.
- End-to-end installer tests (`tests/test_installers.py`) — bash on
  POSIX, pwsh + cmd.exe on Windows CI.

### Added — System metrics

- Windows temperature: non-admin path via
  `Win32_PerfFormattedData_Counters_ThermalZoneInformation` (perf
  counter, no elevation), with admin-only
  `MSAcpi_ThermalZoneTemperature` fallback for service-mode deploys.
- macOS temperature: optional `osx-cpu-temp` brew binary (opt-in;
  falls back to 0.0 silently when not installed).

### Added — Docs & deps

- `certifi>=2024.2.2` as an explicit dependency.
- Pipeline reference page in the mkdocs nav.

### Changed — Transport

- `ZenohClient._build_config` gates TLS injection on `tls/` endpoint
  scheme rather than mode, so peer-of-router setups verify outbound
  TLS too.
- `get_or_create_zenoh_session` no longer provisions a local `zenohd`
  binary in pure client mode — install only runs for
  `mode in ("router", "peer")`.
- Router config generator (`infra/zenoh.py`) injects
  `timestamping.enabled.router: true` into `generated_router.json5`
  so rocksdb storage receives the `data_info` column-family records
  it needs.

### Changed — Plugin install

- `language_model` and `audio_transcriber` install scripts
  early-return silently when the binary is already at the pinned tag,
  eliminating banner-log noise on every agent start.
- Component registry: `Running custom install script for {name}…`
  demoted INFO → DEBUG; plugin's own logs are the sole signal when
  work happens.

### Changed — Onboarding

- `install.ps1` translates PowerShell-idiomatic params
  (`-DeviceName`, `-Email`, `-RegistrationKey`) into argparse
  kebab-case before invoking `main.py install`. Propagates
  `$LASTEXITCODE` so installer crashes no longer return 0 silently.

### Changed — Dependencies & docs

- `pyproject.toml` `dependencies` cleaned: dropped stale entries;
  verified against the wire deps now in use.
- `NOTICE.md` rewritten to reflect the actual direct dependency set
  with optional/dev sections.
- `THIRDPARTYLICENSES` and `THIRDPARTYNOTICES` regenerated from the
  current venv via `pip-licenses` — stale entries (`opencv-python`,
  `pillow`, `tensorflow_cpu`, `sounddevice`, `python-dotenv`)
  removed.

### Removed — JIT config

- `update_applied_agent_config` POST from `main.py` JIT-onboarding
  path and from `runtime.py:_deploy_model`. Backend learns applied
  config via Zenoh status events instead.

### Removed — Windows deps

- `wmi` Windows-only dependency. The Python lib's `pywin32`
  postinstall is fragile under uv (dist-info lands, module doesn't)
  and creates COM objects whose destructors spammed
  `Win32 exception releasing IUnknown` lines at process teardown.
  Replaced with PowerShell shell-out.

### Fixed — Installers

- `install.cmd` argv shim: `shift` inside a parenthesized `if` block
  didn't update `%1..%9` (cmd parses positionals at block-entry).
  Caused python to be invoked with `run main.py install …`, fail
  silently, and return 0 to the caller. Rewritten with
  delayed-expansion `%*` strip.
