# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""CLI entry point: dispatches run, stop, reset, self-check, install-plugin subcommands."""

import argparse
import logging
import os
import shutil
import sys
import tomllib
from fnmatch import fnmatch
from pathlib import Path

from link import constants
from link.app.onboarding import FLEET_MARKER_PATH, activate_device, enroll_device, register_device
from link.app.runtime import AgentRuntime
from link.app.state import StateManager
from link.app.updater import (
    BundleUpdateError,
    ReleaseNotFound,
    running_frozen_bundle,
    swap_bundle,
)
from link.config.loader import load_config
from link.config.models import AgentConfig
from link.infra.service import ServiceManager
from link.infra.zenoh import get_or_create_zenoh_session
from link.utils.logger import setup_logging

logger = setup_logging()


def run(args: argparse.Namespace):
    """Unified entry point for identity resolution.

    1. CLI config provided? -> resume state or bootstrap new session.
    2. No CLI args? -> auto-resume latest session.
    3. No session? -> fall back to default config.
    """
    cwd = Path.cwd().absolute()
    state_manager = StateManager()
    agent_config = None

    # --- PHASE 1: RESOLVE IDENTITY ---

    # A. CLI Override
    if args.config:
        config_path = (cwd / args.config).absolute()
        is_session_file = config_path.name.startswith("session_")

        if is_session_file:
            # Resume existing session
            loaded_state = state_manager.load_state(explicit_path=config_path)
            if loaded_state:
                logger.info(f"Resuming from specific session: {config_path.name}")
                try:
                    agent_config = AgentConfig(**loaded_state)
                except Exception as e:
                    logger.critical(f"State Load Failed: {e}")
                    sys.exit(1)
            else:
                logger.critical(f"Session file not found: {config_path}")
                sys.exit(1)
        else:
            # Bootstrap from raw config
            logger.info(f"Bootstrapping from user config: {config_path.name}")
            try:
                agent_config = load_config(config_path)
                state_manager.bootstrap(agent_config)
            except Exception as e:
                logger.critical(f"Config Load Failed: {e}")
                sys.exit(1)

    # B. Auto-Resume
    elif (saved_state := state_manager.load_state()) is not None:
        logger.info(f"Resuming from latest session: {state_manager.current_session_path}")
        try:
            agent_config = AgentConfig(**saved_state)
        except Exception as e:
            logger.warning(f"State corrupted ({e}).")

    # C. Fallback ladder: JIT onboarding → fleet-marker fail-loud → factory defaults.
    if agent_config is None:
        api_url = args.api_url or constants.DEFAULT_API_URL
        if args.registration_key:
            try:
                if args.device_name and (args.email or args.token):
                    agent_config = register_device(
                        name=args.device_name,
                        reg_key=args.registration_key,
                        api_url=api_url,
                        email=args.email,
                        password=args.password,
                        token=args.token,
                    )
                elif args.device_id:
                    agent_config = activate_device(
                        device_id=args.device_id,
                        reg_key=args.registration_key,
                        api_url=api_url,
                    )
                else:
                    logger.critical("Onboarding requires (--device-name AND --email/--token) OR (--device-id)")
                    sys.exit(1)
                state_manager.bootstrap(agent_config)
            except Exception as e:
                logger.critical(f"Onboarding failed: {e}", exc_info=True)
                sys.exit(1)
        elif args.fleet_key:
            try:
                agent_config = enroll_device(fleet_key=args.fleet_key, api_url=api_url)
                state_manager.bootstrap(agent_config)
            except Exception as e:
                logger.critical(f"Fleet enrollment failed: {e}", exc_info=True)
                sys.exit(1)
        elif FLEET_MARKER_PATH.exists():
            # Wiped fleet device: refuse to silently re-bootstrap as a fresh agent.
            logger.critical(
                "This device was fleet-enrolled but no local session was found. "
                "Re-run enrollment with --fleet-key (normally done by the partner installer)."
            )
            sys.exit(1)
        else:
            logger.info("Initialising from default configuration.")
            try:
                agent_config = load_config(Path("configs/default_config.json").absolute())
                state_manager.bootstrap(agent_config)
            except Exception as e:
                logger.critical(f"Default Config Load Failed: {e}")
                sys.exit(1)

    # --- PHASE 2: DEPLOYMENT ---
    if args.headless:
        _deploy_service(cwd)
        return

    # --- PHASE 3: RUNTIME ---
    logger.info(f"Starting Agent (Device: {agent_config.identity.device_id})...")

    # A. Infrastructure Initialization
    zenoh_session = None

    # Explicitly check if the user configured a Zenoh transport
    if agent_config.transport and agent_config.transport.type == "zenoh":
        try:
            # Use the factory function to get a session
            zenoh_session = get_or_create_zenoh_session(agent_config.transport)
        except Exception as e:
            logger.critical(f"Zenoh Connection Failed: {e}")
            sys.exit(1)

    # B. Logger
    # Pass the session (if any) so the logger can attach Zenoh handlers
    logger.info(f"Logging directed to targets: {[h.type for h in agent_config.logging.handlers]}")
    setup_logging(agent_config.logging, agent_config.reporting, zenoh_session=zenoh_session)

    # C. Execution
    runtime = AgentRuntime(agent_config, state_manager, zenoh_session)
    try:
        runtime.run()
    except KeyboardInterrupt:
        logger.info("Agent stopped by user.")
    except Exception as e:
        logger.critical(f"Runtime crash: {e}")
        sys.exit(1)
    finally:
        # Flush logging handlers BEFORE closing Zenoh: the offline lifecycle message
        # queued by AsyncZenohHandler must drain against an open session.
        try:
            logging.shutdown()
        except Exception:
            pass
        if zenoh_session:
            zenoh_session.close()

    # D. OTA Update / Config restart & Re-exec
    # - `update_requested` → pull latest code + refresh binaries, then execv.
    # - `config_restart_requested` → execv only (no git pull) to pick up a
    #   persisted-but-unapplied AgentConfig after a hot-swap failure.
    # Code update takes priority; if both are set, git pull covers the config too.
    if runtime.update_requested:
        _apply_update_and_reexec()
    elif runtime.config_restart_requested:
        logger.info("Restarting agent to pick up persisted config...")
        os.execv(sys.executable, [sys.executable] + sys.argv)


def self_check(args: argparse.Namespace) -> int:
    """Boot the runtime to config + transport + plugins, then exit cleanly.

    Sole consumer: ``bundle_updater.health_check()``. Run on a freshly extracted
    bundle before flipping ``current`` to prove the new binary can import, parse
    the active session, open its transport, and enumerate plugin entry points; no
    pipelines start and no inference runs. Exit 0 = healthy, nonzero = failure
    (caller rolls back the flip).
    """
    state_manager = StateManager()
    saved_state = state_manager.load_state()
    if saved_state is None:
        # No session means nothing meaningful to check against. The bootstrap
        # path (Pattern B) hits this and has its own verification at fetch time;
        # the OTA path always has a session.
        logger.info("self-check: no session found — binary boot only.")
        try:
            from link.components.registry import ComponentRegistry

            ComponentRegistry._refresh_entry_points()
        except Exception as e:
            logger.error(f"self-check: plugin enumeration failed: {e}")
            return 1
        return 0

    try:
        agent_config = AgentConfig(**saved_state)
    except Exception as e:
        # Log the exception class only: the message can include field values
        # from saved_state (identity tokens, api keys) we don't want logged.
        logger.error("self-check: session present but unparseable (%s)", type(e).__name__)
        return 1

    zenoh_session = None
    if agent_config.transport and agent_config.transport.type == "zenoh":
        try:
            zenoh_session = get_or_create_zenoh_session(agent_config.transport)
        except Exception as e:
            logger.error(f"self-check: Zenoh open failed: {e}")
            return 1

    try:
        from link.components.registry import ComponentRegistry

        ComponentRegistry._refresh_entry_points()
    except Exception as e:
        logger.error(f"self-check: plugin enumeration failed: {e}")
        return 1
    finally:
        if zenoh_session is not None:
            try:
                zenoh_session.close()
            except Exception:
                pass

    logger.info(f"self-check: ok (device={agent_config.identity.device_id})")
    return 0


def _apply_update_and_reexec():
    """Apply a bundled OTA: swap_bundle downloads, verifies, health-checks, and
    atomically flips ``current``, then exits 42 for the launcher to respawn. We
    never execv here because a frozen ``sys.executable`` points at the old
    version being replaced. Source installs are developer-only and update via
    ``git pull``, so OTA is declined there."""
    if not running_frozen_bundle():
        logger.info("Source install: OTA disabled (update via git manually); ignoring request.")
        return

    logger.info("Applying OTA update...")
    try:
        swap_bundle()
    except ReleaseNotFound as e:
        # Version published but its per-platform asset isn't up yet: relaunch
        # current (42) and retry next poll.
        logger.info(f"Update not ready yet ({e}); relaunching current, will retry.")
        sys.exit(42)
    except BundleUpdateError as e:
        logger.critical(f"Bundle update failed: {e}")
        sys.exit(1)
    except Exception as e:
        # Route disk-full/network panics through the same graceful exit so the
        # launcher doesn't read an uncaught traceback as a rollback trigger.
        logger.critical(f"Bundle update failed with unexpected error ({type(e).__name__}): {e}")
        sys.exit(1)
    # Always exit 42: whether we flipped or were already at latest, the launcher
    # respawns from `current`.
    logger.info("Exiting (code 42) for launcher to respawn from current.")
    sys.exit(42)


def _deploy_service(cwd: Path):
    """Install the agent as a service."""
    logger.info("Deploying Agent Service...")

    python_exe = cwd / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    cmd = f"{python_exe} main.py run"

    svc_env: dict[str, str] = {"PYTHONUNBUFFERED": "1"}

    agent = ServiceManager(
        "locai-link",
        cmd,
        "Loc.ai Agent",
        str(cwd),
        env_vars=svc_env,
    )

    try:
        if agent.is_installed():
            agent.stop()
            agent.uninstall()

        agent.install(start_now=True)
        logger.info("Service deployed. It will pick up the current configuration automatically.")
    except Exception as e:
        logger.critical(f"Service deployment failed: {e}")
        sys.exit(1)


def stop():
    """Stops all services."""
    for svc in ["locai-link", "zenohd"]:
        try:
            manager = ServiceManager(svc)
            if manager.is_running():
                manager.stop()
        except Exception:
            pass


def _find_link_repo_root(start: Path) -> Path | None:
    """Locate the locai-link repo root at or above ``start``, or None.

    Walks upward for a ``pyproject.toml`` that identifies the locai-link project
    (project name ``locai-link``, or an ``src/link`` package as a fallback).
    """
    for candidate in (start, *start.parents):
        pyproject = candidate / "pyproject.toml"
        if not pyproject.exists():
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        project = data.get("project")
        if isinstance(project, dict) and project.get("name") == "locai-link":
            return candidate
        if (candidate / "src" / "link").is_dir():
            return candidate
    return None


def reset(hard: bool = False):
    """Nuke the environment recursively; ``hard`` also deletes session files."""
    logger.info("Resetting environment...")

    # Stop & Uninstall
    try:
        stop()
        ServiceManager("locai-link").uninstall()
        ServiceManager("zenohd").uninstall()
    except Exception:
        pass

    # Anchor the destructive walk to the locai-link repo root
    root = _find_link_repo_root(Path.cwd().absolute())
    if root is None:
        logger.critical(
            "reset refused: current directory is not inside a locai-link repository "
            "(no locai-link pyproject.toml here or in any parent). "
            "cd into the repo checkout and retry."
        )
        sys.exit(1)

    logger.info(f"Resetting environment under {root}")

    # Configuration
    # Do NOT descend into these
    exclude_dirs = {".git", ".vscode", ".github", "docs", "node_modules", "target"}

    # Directories to nuke (exact matches)
    target_dirs = {
        ".venv",
        ".zenoh",
        "__pycache__",
        "logs",
        "link_db",
        "dist",
        "build",
        ".pytest_cache",
        "site",
        ".benchmarks",
        ".ruff_cache",
    }

    # Files to nuke (glob patterns)
    target_files = {"uv.lock", ".coverage"}
    if hard:
        target_files.add("session_*.json")

    count = 0

    # Efficient Recursive Walk
    for dirpath, dirs, files in os.walk(root, topdown=True):
        # 1. Prune Exclusions (Modify dirs in-place to skip traversing ignored trees)
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        # 2. Remove Target Directories
        # Iterate over a copy so we can safely modify 'dirs'
        for d in list(dirs):
            if d in target_dirs:
                try:
                    shutil.rmtree(Path(dirpath) / d)
                    count += 1
                    # Vital: Remove from 'dirs' so os.walk doesn't try to enter it
                    dirs.remove(d)
                except Exception:
                    pass

        # 3. Remove Target Files
        for f in files:
            if any(fnmatch(f, pattern) for pattern in target_files):
                try:
                    (Path(dirpath) / f).unlink()
                    count += 1
                except Exception:
                    pass

    logger.info(f"Reset complete. Removed {count} items.")


def main():
    """CLI entry point: parses arguments and dispatches to subcommands."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    # Lifecycle
    run_p = subparsers.add_parser("run", help="Runs the agent.")
    run_p.add_argument("--config", help="Path to a config file OR a session state file.")
    run_p.add_argument(
        "--headless",
        action="store_true",
        help="Run as a background service (no foreground process).",
    )
    run_p.add_argument("--registration-key", help="One-time key for onboarding.")
    run_p.add_argument("--device-name", help="Device name for onboarding.")
    run_p.add_argument("--device-id", help="Existing device ID for re-activation.")
    run_p.add_argument("--email", help="Platform email for authentication.")
    run_p.add_argument("--password", help="Platform password (prompted securely if omitted).")
    run_p.add_argument("--token", help="Pre-obtained JWT token (alternative to email/password).")
    run_p.add_argument("--api-url", help="Override API URL.")
    run_p.add_argument(
        "--fleet-key",
        help="Org-scoped fleet enrollment key; accepts the key itself or file:<path>.",
    )

    subparsers.add_parser("stop", help="Stops all running services.")
    subparsers.add_parser("reset", help="Resets the environment.").add_argument("--hard", action="store_true")

    # Self-check: minimal boot used by the OTA health check in bundle_updater.
    subparsers.add_parser(
        "self-check",
        help="Boot to config+transport+plugins and exit 0 if healthy. Used by OTA rollback.",
    )

    subparsers.add_parser("install-plugin", help="Installs a plugin.").add_argument("name")

    args = parser.parse_args()

    if args.command == "stop":
        stop()
    elif args.command == "reset":
        reset(args.hard)
    elif args.command == "run":
        run(args)
    elif args.command == "install-plugin":
        from link.components.registry import ComponentRegistry

        ComponentRegistry.install_plugin(args.name)
    elif args.command == "self-check":
        sys.exit(self_check(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
