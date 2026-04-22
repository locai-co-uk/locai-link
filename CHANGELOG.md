# Changelog

All notable changes migrating from `locai-link-old` to `locai-link-new`.

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
