# Changelog

All notable changes. Newest at top. The migration narrative from
`locai-link-old` is preserved below the per-version entries as historical
record.

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

Cuts over inference observability to HTTP-response interception, lands the
bundling subsystem (PyInstaller + macOS notarisation + drag-to-extract),
hardens llama-swap orphan handling, and ships the typed command-wire
contract.

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

### Added — Bundling subsystem

- `bundling/` directory with PyInstaller spec, manifest writer,
  pre-fetch step for plugin binaries (`llama.cpp` + `whisper.cpp`),
  macOS code-signing helper, entitlements, and a single CLI entry
  `bundling/build.py`. Asset name is derived from `--plugins` (e.g.
  `locai-link-llm-stt-darwin-arm64.dmg`); no curated profile files.
- `.github/workflows/bundle.yml` — bundles on every PR that touches
  bundling, plugin install scripts, or runtime code. Matrixed across
  Ubuntu / Windows / macOS.
- `.github/workflows/release.yml` — consolidated single-workflow
  release: builds, signs, notarises (macOS), uploads artifacts to a
  GitHub Release in one run. Previous two-workflow split
  (`release.yml` + `release-assets.yml`) is gone, with it the
  `RELEASE_TOKEN` PAT requirement.
- macOS path: Developer ID Application signing of every binary in the
  bundle, hardened-runtime entitlements, notarisation via `notarytool`,
  drag-to-extract DMG packaging. Verified end-to-end on Apple Silicon.
- Windows: prepared but unsigned (signtool wiring deferred — see
  `TODO.md`).

### Added — llama-swap orphan handling

- `state/swap_<port>.pid` — every llama-swap process Link spawns writes
  its PID to this file. On next start, `_reclaim_previous_instance()`
  reads the pidfile, verifies via `psutil` cmdline that the PID is
  actually llama-swap (refuses to touch foreign processes that may
  have reused the PID), and terminates cleanly before binding the
  port. If the port is held by a non-llama-swap process, Link raises a
  diagnostic `RuntimeError` listing the platform-appropriate `lsof` /
  `netstat` command — no longer kills random processes.

### Added — Command contract

- `UninstallModelCommand` and `UpdatePipelineCommand` to the typed
  command schema (`src/link/config/commands.py`).
- `AgentRuntime._update_pipeline()` — applies an updated `PipelineConfig`
  in place, restarting the pipeline if running.
- `tests/test_command_wire_contract.py` + `tests/fixtures/wire/*.json` —
  golden-fixture round-trip tests for every command type. Mirrors the
  backend's `to_wire` contract.

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

### Changed

- The CORS shim has been renamed and broadened into `ServingProxy`. The
  old name (`CorsProxy`, in `src/link/infra/cors_proxy.py`) is gone;
  the new file is `src/link/infra/serving_proxy.py`. CORS is now one of
  two optional features the proxy provides (the other being telemetry
  capture); both are independently configurable.
- `SwapManager` always fronts llama-swap with `ServingProxy`,
  regardless of CORS state. llama-swap binds the loopback-only listen
  port (`public_port + 50`); the proxy owns the public port.
  Previously the proxy was only instantiated when CORS was on.
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
- `runtime.py` START_SERVING handler: `alias = cmd.model_display_name`.
  llama-swap routes by the human-readable display name; the canonical
  pipeline_id (UUID) is used independently in the Zenoh sink topic
  (`locai/devices/<device_id>/models/<pipeline_id>/results`) and so
  carries through to Firestore + PostHog for backend attribution. The
  two id spaces are deliberately separate.
- The language_model adapter no longer log-parses llama-swap stdout
  for inference timing; telemetry now flows through the ServingProxy's
  response interception. `ModelServer`'s log-parse path is preserved
  as the legacy fallback for the (rare) case where llama-swap isn't
  installed.
- `bundling/manifest.py` replaces the old `bundle_profile.py`
  YAML-driven profile machinery. The build CLI takes a flat
  `--plugins` list; the manifest is the authoritative record of
  what's in a bundle.
- Plugin install scripts are quieter when binaries are already
  installed at the pinned tag — early-return without log noise.

### Removed

- `src/link/infra/cors_proxy.py` (renamed to `serving_proxy.py`;
  imports updated across the repo).
- `bundling/bundle_profile.py` and the YAML profile artefacts. The
  build CLI's `--plugins` flag is the canonical input.
- `AgentRuntime._normalise_command` and `_map_runtime_to_pipeline_config`
  — the backend now sends ready-made pipelines, the agent does no
  derivation.
- The legacy `REMOVE_MODEL` command alias. Use `UNINSTALL_MODEL`.
- The `--profile` flag from `bundling/build.py` and any references to
  partner-named bundles.
- The `RELEASE_TOKEN` PAT requirement — the consolidated release
  workflow uses the default `GITHUB_TOKEN`.

### Fixed

- llama-swap orphans surviving an unclean Link shutdown — `_start()`
  now reliably reclaims its own previous instance via pidfile and
  refuses to touch anything else.
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

## [1.0.9] — 2026-05-08

### Added
- `LinkReporter.report_deployment_progress()` — incremental model deployment events (`downloading`, `configuring`, `completed`) with byte counts; throttled to 5% steps.
- `AgentCommand._seen` deque + `mark_seen()` — bounded `command_id` dedup to support online-reconcile flows where Firestore HTTP backlog and live Zenoh inbox samples overlap.
- Zenoh client: `tls_root_ca`, `username`, `password` args in `transport.args`. `tls_root_ca: "auto"` resolves to the `certifi` bundle at runtime — no PEM file needs to live on disk.
- One-liner installers (`install.sh`, `install.cmd`) honor `--branch` and `--repo-url` CLI args, matching `install.ps1`. Env vars (`LOCAI_BRANCH`, `LOCAI_REPO_URL`) still respected; CLI overrides them.
- Windows temperature: non-admin path via `Win32_PerfFormattedData_Counters_ThermalZoneInformation` (perf counter, no elevation), with admin-only `MSAcpi_ThermalZoneTemperature` fallback for service-mode deploys.
- macOS temperature: optional `osx-cpu-temp` brew binary (opt-in; falls back to 0.0 silently when not installed).
- `certifi>=2024.2.2` as explicit dependency.
- Pipeline reference page in mkdocs nav.
- End-to-end installer tests (`tests/test_installers.py`) — bash on POSIX, pwsh + cmd.exe on Windows CI.

### Changed
- `ZenohClient._build_config` gates TLS injection on `tls/` endpoint scheme rather than mode, so peer-of-router setups verify outbound TLS too.
- `get_or_create_zenoh_session` no longer provisions a local `zenohd` binary in pure client mode — install only runs for `mode in ("router", "peer")`.
- Plugin install scripts (`language_model`, `audio_transcriber`) early-return silently when the binary is already at the pinned tag, eliminating banner-log noise on every agent start.
- Component registry: `Running custom install script for {name}…` demoted INFO → DEBUG; plugin's own logs are the sole signal when work happens.
- `install.ps1` translates PowerShell-idiomatic params (`-DeviceName`, `-Email`, `-RegistrationKey`) into argparse kebab-case before invoking `main.py install`. Propagates `$LASTEXITCODE` so installer crashes no longer return 0 silently.
- Router config generator (`infra/zenoh.py`) injects `timestamping.enabled.router: true` into `generated_router.json5` so rocksdb storage receives the `data_info` column-family records it needs.
- pyproject `dependencies` cleaned: dropped stale entries; verified against the wire deps now in use.
- `NOTICE.md` rewritten to reflect the actual direct dependency set with optional/dev sections.
- `THIRDPARTYLICENSES` and `THIRDPARTYNOTICES` regenerated from the current venv via `pip-licenses` — stale entries (`opencv-python`, `pillow`, `tensorflow_cpu`, `sounddevice`, `python-dotenv`) removed.

### Removed
- `update_applied_agent_config` POST from `main.py` JIT-onboarding path and from `runtime.py:_deploy_model`. Backend learns applied config via Zenoh status events instead.
- `wmi` Windows-only dependency. The Python lib's `pywin32` postinstall is fragile under uv (dist-info lands, module doesn't) and creates COM objects whose destructors spammed `Win32 exception releasing IUnknown` lines at process teardown. Replaced with PowerShell shell-out.

### Fixed
- `install.cmd` argv shim: `shift` inside a parenthesized `if` block didn't update `%1..%9` (cmd parses positionals at block-entry). Caused python to be invoked with `run main.py install …`, fail silently, and return 0 to the caller. Rewritten with delayed-expansion `%*` strip.

---

## Architecture

### Changed
- **Single-process model.** The old two-process split (`manager.py` supervisor + `agent.py` worker) has been folded into a single `main.py`. OTA updates now use `os.execv()` to replace the process in place rather than a parent supervisor loop — the PID is preserved across updates.
- **Pipeline-based runtime.** The monolithic `agent.py` (1,496 lines) handling command polling, inference dispatch, serving, and metrics has been replaced by `AgentRuntime` + `Pipeline` threads. Each pipeline is a `Source → Sink` pair running on its own thread, composable from config.
- **Component registry.** Components (HTTP sources, Zenoh sinks, system monitors, command handlers) self-register via a `@ComponentRegistry.register("name")` decorator and are instantiated from declarative config.
- **Pydantic config models.** Replaced ad-hoc JSON config handling with typed `AgentConfig`, `PipelineConfig`, `TransportConfig`, `GenericConfig` Pydantic models. Schema version pinned at `2.1`.
- **Session state persistence.** New `StateManager` writes timestamped `configs/session_*.json` files for crash recovery. The agent auto-resumes the latest session on restart; running pipelines are automatically re-started.

### Removed
- `manager.py` (1,080 lines) — subcommands folded into `main.py`.
- `src/link/serving/` — `LLMServer`, `WhisperServer`, `BaseServer` moved into plugins.
- `src/link/inference/` — `dispatcher.py` and TFLite runners (`language_model_gguf.py`, `image_detection_cpy_tflite.py`, `audio_classification_yamnet_tflite.py`) replaced by plugin adapters.
- `src/link/logger/` custom module — replaced by `src/link/utils/logger.py` with structured async handlers.
- `src/link/analytics.py` — analytics now flow through the generic reporting handler system.
- `src/link/components/buffers.py` — unused `LocalBuffer` stub.

## Registration & Onboarding

### Added
- **Four-tier identity resolution** in `main.py run`: explicit `--config`, auto-resume latest session, just-in-time onboarding with `--registration-key`, or factory defaults.
- **`activate_device()`** for re-activating existing devices with just `--device-id` + `--registration-key`.

### Changed
- The `register_device()` function previously took `--username` and sent it in the request body. It now takes `--email` (or a pre-obtained `--token`) and uses `login_and_get_token()` to obtain a JWT, which is then sent as `Authorization: Bearer` on the `/devices/register-with-key` request — matching the backend's expected flow.
- Passwords are prompted securely via `getpass` when omitted from the command line.

## Installation

### Added
- **One-liner install scripts** for Linux/macOS (`install.sh`), Windows PowerShell (`install.ps1`), and Windows CMD (`install.cmd`). Each bootstraps `uv`, detects local vs remote `main.py`, and hands off to the new `install` subcommand.
- **`main.py install` subcommand** — orchestrates clone/update repo → `setup` → register → run in a single command.

### Changed
- Setup is now `main.py setup` (with `--dev`, `--tui` extras) instead of `manager.py setup --extras`.

## Plugins

### Added
- **Plugin architecture.** Plugins are standalone installable packages that register via the `locai.plugins` entry point. Each has its own `pyproject.toml`, `adapter.py`, and `install.py`.
- **`language_model` plugin** — ports the LLM server logic (`llama-server` lifecycle). Pinned to llama.cpp `b8808`.
- **`audio_transcriber` plugin** — ports the whisper server logic (`whisper-server` lifecycle). Pinned to whisper.cpp `v1.8.4`.
- **`image_classifier` plugin** — vision inference via TFLite.
- **`audio_classifier` plugin** — audio tagging via TFLite.
- **CUDA build-from-source fallback** on Linux when the CUDA toolkit (`nvcc`) is detected — enables `-DGGML_CUDA=ON` for best GPU performance.
- **Tag-based caching** — each plugin install uses a `tag` file to skip re-download/rebuild when already at the pinned version. Re-running `install.py` after an OTA update is cheap.
- **ARM64 Linux support** for llama.cpp prebuilts (old code was x64-only).
- **macOS quarantine stripping** — `xattr -dr com.apple.quarantine` is applied after extraction so Gatekeeper doesn't block binaries.
- **Symlink preservation** when extracting tarballs — versioned shared library names (e.g. `libmtmd.0.dylib`) now resolve correctly.

### Changed
- **No more hardcoded `--chat-template chatml`** — the language_model plugin only passes `--chat-template` when explicitly configured, letting llama.cpp auto-detect from the model's metadata.
- **Health-check timeout raised from 30s to 120s** for plugin servers — large models on CPU can take longer to load.
- **LD_LIBRARY_PATH now walks subdirectories** to find `ggml*.so` / `whisper*.so` files (CUDA shared libs live in nested folders).

## Configuration

### Added
- **Zenoh transport** as an alternative to HTTP for the control plane and pipelines. `TransportConfig.type` = `"zenoh"` or `"http"`.
- **Declarative pipelines in config** — `pipelines: [{id, source, sink, active}]`. Sources and sinks are instantiated by name from the component registry.
- **Config-driven logging and reporting handlers** — `LoggingConfig.handlers` and `ReportingConfig.handlers` accept a list of typed handler configs (console, http, zenoh).
- **Template substitution** in handler args — `${identity.device_id}`, `{cid}`, `{mid}` are resolved at runtime so a single config serves many devices.

## Pipelines

### Changed
- **`AgentCommand` sink returns `True` on empty input** (was `None`). The pipeline loop treats non-truthy sink results as failures and warns, so idle poll ticks (`http_poll` returning `[]` because no commands are pending) used to produce a spurious `"Sink is returning False"` warning. An empty dispatch is now a successful no-op.

## Transport, Logging & Reporting

### Added
- **`LinkReporter`** — custom logger class exposing `report_lifecycle(status)`, `report_command(cmd_id, status, output)`, `report_model(...)` for structured status reporting.
- **`AsyncHandler` base class** with a worker thread and queue — all handlers are non-blocking. Subclasses:
  - `AsyncHTTPHandler` — routes events to HTTP endpoints via template lookup (PUT for `lifecycle_status`, POST for everything else).
  - `AsyncZenohHandler` — publishes to Zenoh topics.
- **`HttpError` exception class** with `status`, `reason`, and `retryable` fields — lets callers distinguish transient failures (timeout, 5xx, connection refused) from non-retryable ones (401, 403, 404). The old `HttpClient` swallowed every error as `None`/`False`.
- **`agent_version` in lifecycle status payload** — `report_lifecycle()` now includes the agent's installed version (read from `importlib.metadata.version("locai-link")`), matching the backend's `AgentStatusUpdate.agent_version` semver requirement.

### Changed
- `HttpClient.get()` / `post()` now classify errors: timeouts/5xx/connection errors return `None`/`False` (retryable); 4xx auth/client errors raise `HttpError`. `HttpPoller` and `HttpPublisher` catch `HttpError` and log with actionable context before re-raising.
- **HTTP log payload shape** now matches the backend's `LogCreate` schema: `{message, severity, category}`. Severity is lowercase (`DEBUG` maps to `"info"`); category defaults to `"other"` and can be classified via `logger.info("msg", extra={"category": "security"})`. Replaces the previous `{timestamp, level, message, logger}` shape.
- **`AsyncHTTPHandler` now retries** timeouts, connection errors, and 5xx responses with exponential backoff (`0.5s`, `1.5s`, capped at 2 retries). 4xx responses stay fatal so they aren't retried indefinitely. Timeout is configurable per-handler via `args.timeout` (default `10s`) and split into `(connect=3s, read=timeout)` — fast-fail on unreachable hosts, tolerant on slow responses.
- **`HttpClient.get()` timeout** demoted from warning to debug — polling is self-healing (next tick retries) and a flaky network was producing console spam at warning level.

## Service Deployment

### Added
- **Cross-platform service manager** (`src/link/infra/service.py`) — `ServiceManager` factory picks the right backend:
  - Linux → systemd user service at `~/.config/systemd/user/locai-link.service`
  - macOS → LaunchAgent plist at `~/Library/LaunchAgents/io.locai.locai-link.plist`
  - Windows → Windows Service via `sc.exe` (requires admin)
- **`main.py run --prod`** installs and starts the agent as an OS service.
- **`main.py stop`** gracefully stops the agent (and `zenohd` if installed).

## OTA Updates

### Added
- **`UPDATE_AGENT` command** handled by `AgentRuntime`. On receipt: reports completion, shuts down pipelines cleanly, signals `main.py` to update.
- **`src/link/app/updater.py`** — `pull_and_update()` (git fetch/stash/pull/pop + `uv pip install -e .`), `reinstall_plugin_binaries()` (config-driven, see Changed below), `get_current_branch()`, `get_local_version()`.
- **In-place restart via `os.execv()`** — the process image is replaced but the PID is preserved. systemd/launchd see a continuously running process with no downtime gap.
- **Branch-aware updates** — dev branches pull from `origin/<current-branch>`, not `origin/main`.
- **Stash-safe updates** — dirty working trees are stashed and reapplied around the pull.
- **`-DGGML_NATIVE=OFF` on macOS** for both llama.cpp and whisper.cpp builds — avoids ggml's `-mcpu=native` fallback, which AppleClang rejects on arm64. Metal + Accelerate carry the perf-critical paths on Apple Silicon, so there's no throughput regression. Linux/Windows unchanged.
- **Silenced detached-HEAD git advisory** — tagged clones now pass `-c advice.detachedHead=false` inline to remove cosmetic noise from OTA build logs.

### Changed
- **`reinstall_plugin_binaries()` is now config-driven.** Previously, every plugin under `plugins/` had its `install.py` re-run on every OTA — so a device running only `language_model` still tried to build whisper.cpp, TFLite, etc. Now the updater walks the active `AgentConfig.pipelines[*].source/sink.type`, maps each type to its owning plugin via that plugin's `[project.entry-points."locai.plugins"]` in `pyproject.toml`, and only refreshes plugins whose declared entry-point names are actually referenced. Plugins not in use are silently skipped.

### Removed
- The old `EXIT_CODE_UPDATE = 42` + subprocess-loop supervisor in `manager.py`. The new architecture needs no external supervisor.

## Testing & CI

### Added
- **77 unit tests** covering HTTP client error classification, onboarding auth flow, state manager version handling, OTA updater logic, runtime command handling, service manager across all three OSes, Zenoh router, config loading, and platform detection.
- **`ci` pytest marker** for tests that need external binaries or network — skipped locally by default, enabled in CI via `-m ""` override.
- **Integration tests** in each plugin directory — download real models, spawn real server binaries, verify full transcription/completion flow.
- **Multi-OS CI matrix** (Ubuntu, macOS, Windows) on unit and integration jobs.
- **Version-bump gate** on PRs — fails if `pyproject.toml` version hasn't been bumped.
- **`audio_transcriber` wired into the integration-test job** alongside the other three plugins.

## CLI Reference

### Before (old `manager.py`)
```
manager.py install        # Full installation wizard
manager.py setup          # Configure venv and deps
manager.py reset          # Clean up artifacts
manager.py register       # Register device
manager.py activate       # Activate a pre-registered device
manager.py update         # Pull latest code
manager.py run            # Run agent (supervisor loop)
manager.py install-deps   # Install llama/whisper server binaries
```

### After (new `main.py`)
```
main.py setup             # Install Python dependencies (--dev, --tui)
main.py install           # Full installation wizard
main.py run               # Run agent (in-process, handles OTA via execv)
main.py stop              # Stop all services
main.py reset             # Clean up environment (--hard)
main.py install-plugin    # Install a plugin by name
main.py tui               # Launch text UI (optional)
```

## Breaking Changes

- Registration arg is `--email` (not `--username`).
- Config schema version is `2.1`. Earlier state files are rejected.
- There is no `manager.py`; all commands run via `main.py`.
- Plugins must be installed separately as editable packages (`uv pip install -e "plugins/<name>"`). They are not bundled with the core agent.
- Agent status, command status, and model status payloads flow through the new `LinkReporter` handler system — direct `requests.post` calls to `/agent/{device_id}/status` have been removed.
- HTTP log payload shape changed from `{timestamp, level, message, logger}` to `{message, severity, category}` to match the backend's `LogCreate` schema. Backends consuming `/logs` must accept the new shape; the old field names are no longer emitted.
