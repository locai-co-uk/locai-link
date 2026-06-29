# Changelog

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

## [Unreleased]

Lays down the over-the-air update path for bundled installs. Until now,
updating a frozen Link install meant re-running the host app's installer
or swapping the binary by hand. From this release on, a running bundled
agent can fetch a newer Link off GitHub, swap to it, restart, and — if
the new version doesn't come up cleanly — roll itself back to the
previous one without anyone watching. Developer (source) installs still
update by `git pull` as before.

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
- **Dry-run harness.** `bundling/_dryrun_ota.py` reproduces a v1.0.15
  → v1.0.16 swap end-to-end against a fake release on local disk:
  builds a "current" install, simulates the OTA, asserts the flip, the
  stamp, the GC, and the post-flip layout. `--target /some/path` keeps
  the workspace around so you can inspect it.
- **Integration tests.** 52 Python tests across the source and bundle
  paths; 11 Rust integration tests in the launcher covering exec
  dispatch, exit 42 respawn, and all five rollback conditions
  (within-window, past-window, exit 0, previous-version missing,
  pointer-file shape).

### Not in this release

- **Pattern B first-install.** The host installer still needs to drop a
  pre-extracted bundle (Pattern A) or trigger one out of band; the
  launcher's "no `current` → fetch from `boot.json`" path lands next.
- **Health-window heartbeat from the runtime.** Today the launcher
  decides rollback eligibility purely by wall-clock age of the stamp
  (≤ 120s). Hooking the runtime to clear the stamp itself, once it
  passes its first healthy ping, lands when the runtime grows a
  periodic backend ping.
- **Windows.** Code path is in place but unsigned bundles are blocked
  by Windows SmartScreen — code-signing procurement gates Windows GA.
  macOS + Linux are not blocked.
- **Bundled update telemetry to backend.** `bootstrap_*` and `update_*`
  events are specified in the design doc but not yet emitted; they
  ride the existing event publisher when wired up.

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
