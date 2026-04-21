"""CLI entry point — dispatches setup, run, install, reset, stop, TUI subcommands."""

import argparse
import os
import shutil
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

from link.app.onboarding import activate_device, register_device
from link.app.runtime import AgentRuntime
from link.app.state import StateManager
from link.app.updater import pull_and_update, reinstall_plugin_binaries
from link.config.loader import load_config
from link.config.models import AgentConfig
from link.infra.service import ServiceManager
from link.infra.zenoh import get_or_create_zenoh_session
from link.utils.logger import setup_logging

logger = setup_logging()

DEFAULT_API_URL = "https://api.loc.ai/api/v1"
DEFAULT_REPO_URL = "https://github.com/locai-co-uk/locai-link.git"
DEFAULT_BRANCH = "main"


def setup(args: argparse.Namespace):
    """Lightweight Setup: Only installs Python dependencies.

    Args:
        args (argparse.Namespace): The parsed command line arguments.
    """
    logger.info("Setting up Loc.ai Python Environment")
    install_targets = []
    if args.tui:
        install_targets.append("tui")
    if args.dev:
        install_targets.append("dev")

    cmd = ["uv", "pip", "install", "-e", "."]
    if install_targets:
        cmd[-1] = f".[{','.join(install_targets)}]"

    logger.info(f"Installing: {cmd[-1]}")
    subprocess.run(cmd, check=True)


def install(args: argparse.Namespace):
    """Orchestrator: The 'Web Installer' Logic.

    Handles cloning the repository, setting up the environment, registering the
    device, and optionally starting the agent — all in one command.

    Args:
        args (argparse.Namespace): The parsed command line arguments.
    """
    print("=" * 40)
    print("  Loc.ai Agent Installer")
    print("=" * 40)

    cwd = Path.cwd()

    # Determine API URL
    target_api_url = DEFAULT_API_URL
    if args.api_url:
        target_api_url = args.api_url
        logger.info(f"Using provided API URL: {target_api_url}")
    elif args.dev:
        print("\n--- Development Configuration ---")
        try:
            sys.stdin.flush()
        except Exception:
            pass
        user_input = input("Enter Target API URL: ").strip()
        if not user_input:
            logger.critical("API URL is required when using --dev.")
            sys.exit(1)
        target_api_url = user_input
        logger.info(f"Selected Custom URL: {target_api_url}")

    # Git Operations
    if (cwd / "pyproject.toml").exists():
        install_dir = cwd
        logger.info(f"Detected existing repository in {install_dir}")
        is_fresh_clone = False
    else:
        install_dir = cwd / "locai-link"
        logger.info(f"Target Directory: {install_dir}")
        is_fresh_clone = True

    if is_fresh_clone:
        if install_dir.exists():
            logger.info("Existing installation found — pulling latest changes...")
            subprocess.run(["git", "-C", str(install_dir), "pull", "--ff-only"], check=True)
        else:
            logger.info(f"Cloning repository ({args.branch})...")
            subprocess.run(
                ["git", "clone", "--depth", "1", "-b", args.branch, args.repo_url, str(install_dir)],
                check=True,
            )
    else:
        logger.info("Running from repository — pulling latest changes...")
        subprocess.run(["git", "pull", "--ff-only"], check=True)

    # Interactive Inputs (only prompt for missing values)
    if not args.device_name:
        args.device_name = input("Enter Device Name: ").strip()
    if not args.token and not args.email:
        args.email = input("Enter Email: ").strip()
    if not args.registration_key:
        args.registration_key = input("Enter Registration Key: ").strip()

    identity_provided = args.token or args.email
    if not all([args.device_name, args.registration_key]) or not identity_provided:
        logger.critical("Device name, registration key, and an identity (--email or --token) are required.")
        sys.exit(1)

    # Helper to run commands inside the target repo
    def run_target(cmd_list):
        full_cmd = ["uv", "run", "main.py"] + cmd_list
        subprocess.run(full_cmd, cwd=install_dir, check=True)

    try:
        # A. Setup
        logger.info("Setting up environment...")
        run_target(["setup"])

        # B. Register & Run
        reg_args = [
            "run",
            "--device-name",
            args.device_name,
            "--registration-key",
            args.registration_key,
            "--api-url",
            target_api_url,
        ]
        if args.token:
            reg_args += ["--token", args.token]
        else:
            reg_args += ["--email", args.email]
            # Do NOT pass --password here; onboarding will prompt securely via getpass

        start = args.start_running
        if not start and sys.stdin.isatty():
            confirm = input("\nDo you want to start the agent now? [Y/n] ").strip().lower()
            if confirm in ["", "y", "yes"]:
                start = True

        if start:
            run_target(reg_args)
        else:
            # Register only — run with registration args, then the session is saved for later.
            # We still need to trigger registration, so we do a dry run that will exit after bootstrap.
            logger.info("Registering device...")
            run_target(reg_args)
            print(f"\nInstallation complete. To run later:\n  cd {install_dir}\n  uv run main.py run")

    except subprocess.CalledProcessError as e:
        logger.critical(f"Installation step failed (Exit Code: {e.returncode})")
        sys.exit(e.returncode)


def run(args: argparse.Namespace):
    """Unified Entry Point.

    1. CLI Config Provided? -> Resume State or Bootstrap New Session.
    2. No CLI Args? -> Auto-Resume Latest Session.
    3. No Session? -> Fallback to Default Config.

    Args:
        args (argparse.Namespace): The parsed command line arguments.
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

    # C. JIT Onboarding
    if agent_config is None and args.registration_key:
        api_url = args.api_url if args.api_url else DEFAULT_API_URL
        try:
            if args.device_name and (args.email or args.token):
                agent_config = register_device(
                    name=args.device_name,
                    reg_key=args.registration_key,
                    api_url=api_url,
                    email=args.email,
                    password=getattr(args, "password", None),
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
        except Exception:
            sys.exit(1)

    # D. Factory Defaults
    if agent_config is None:
        logger.info("Initialising from default configuration.")
        try:
            agent_config = load_config(Path("configs/default_config.json").absolute())
            state_manager.bootstrap(agent_config)
        except Exception as e:
            logger.critical(f"Default Config Load Failed: {e}")
            sys.exit(1)

    # --- PHASE 2: DEPLOYMENT ---
    if args.prod:
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
        if zenoh_session:
            zenoh_session.close()
            logger.info("Zenoh session closed.")

    # D. OTA Update / Config restart & Re-exec
    # - `update_requested` → pull latest code + refresh binaries, then execv.
    # - `config_restart_requested` → execv only (no git pull) to pick up a
    #   persisted-but-unapplied AgentConfig after a hot-swap failure.
    # Code update takes priority — if both are set, git pull covers the config too.
    if runtime.update_requested:
        _apply_update_and_reexec(cwd)
    elif runtime.config_restart_requested:
        logger.info("Restarting agent to pick up persisted config...")
        os.execv(sys.executable, [sys.executable] + sys.argv)


def _apply_update_and_reexec(repo_dir: Path):
    """Applies an OTA update and re-execs the current Python process.

    Args:
        repo_dir (Path): The project root (git repository).
    """
    logger.info("Applying OTA update...")
    try:
        pull_and_update(repo_dir)
        reinstall_plugin_binaries(repo_dir)
    except Exception as e:
        logger.critical(f"Update failed: {e}")
        sys.exit(1)

    logger.info("Restarting agent with updated code...")
    # execv replaces this process image in place — same PID, same FDs, same env.
    # Service managers (systemd, launchd) see a continuously running process.
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _deploy_service(cwd: Path):
    """Installs the Agent as a service.

    Args:
        cwd (Path): The current working directory.
    """
    logger.info("Deploying Agent Service...")

    python_exe = cwd / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    cmd = f"{python_exe} main.py run"

    agent = ServiceManager(
        "locai-link",
        cmd,
        "Loc.ai Agent",
        str(cwd),
        env_vars={"PYTHONUNBUFFERED": "1"},
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


def reset(hard: bool = False):
    """Nukes the environment recursively.

    Args:
        hard (bool): If True, deletes session files as well.
    """
    logger.info("Resetting environment...")

    # Stop & Uninstall
    try:
        stop()
        ServiceManager("locai-link").uninstall()
        ServiceManager("zenohd").uninstall()
    except Exception:
        pass

    # Configuration
    exclude_dirs = {".git", ".vscode", ".github", "docs"}

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
    cwd = Path.cwd()

    # Efficient Recursive Walk
    for root, dirs, files in os.walk(cwd, topdown=True):
        # 1. Prune Exclusions (Modify dirs in-place to skip traversing ignored trees)
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        # 2. Remove Target Directories
        # Iterate over a copy so we can safely modify 'dirs'
        for d in list(dirs):
            if d in target_dirs:
                try:
                    shutil.rmtree(Path(root) / d)
                    count += 1
                    # Vital: Remove from 'dirs' so os.walk doesn't try to enter it
                    dirs.remove(d)
                except Exception:
                    pass

        # 3. Remove Target Files
        for f in files:
            if any(fnmatch(f, pattern) for pattern in target_files):
                try:
                    (Path(root) / f).unlink()
                    count += 1
                except Exception:
                    pass

    logger.info(f"Reset complete. Removed {count} items.")


def main():
    """CLI entry point — parses arguments and dispatches to subcommands."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    # Setup
    setup_p = subparsers.add_parser("setup", help="Sets up the environment.")
    setup_p.add_argument("--tui", action="store_true", help="Use the TUI.")
    setup_p.add_argument("--dev", action="store_true", help="Install dev dependencies.")

    # Lifecycle
    run_p = subparsers.add_parser("run", help="Runs the agent.")
    run_p.add_argument("--config", help="Path to a config file OR a session state file.")
    run_p.add_argument("--prod", action="store_true", help="Deploy as a background service.")
    run_p.add_argument("--registration-key", help="One-time key for onboarding.")
    run_p.add_argument("--device-name", help="Device name for onboarding.")
    run_p.add_argument("--device-id", help="Existing device ID for re-activation.")
    run_p.add_argument("--email", help="Platform email for authentication.")
    run_p.add_argument("--password", help="Platform password (prompted securely if omitted).")
    run_p.add_argument("--token", help="Pre-obtained JWT token (alternative to email/password).")
    run_p.add_argument("--api-url", help="Override API URL.")

    # Install (one-liner orchestrator)
    install_p = subparsers.add_parser("install", help="Full installation wizard.")
    install_p.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    install_p.add_argument("--branch", default=DEFAULT_BRANCH)
    install_p.add_argument("--device-name", help="Device name for onboarding.")
    install_p.add_argument("--email", help="Platform email for authentication.")
    install_p.add_argument("--password", help="Platform password (prompted securely if omitted).")
    install_p.add_argument("--token", help="Pre-obtained JWT token (alternative to email/password).")
    install_p.add_argument("--registration-key", help="One-time registration key.")
    install_p.add_argument("--device-type", default="other")
    install_p.add_argument("--start-running", action="store_true", help="Start the agent after installation.")
    install_p.add_argument("--api-url", help="Override API URL.")
    install_p.add_argument("--dev", action="store_true", help="Prompt for custom API URL.")

    subparsers.add_parser("stop", help="Stops all running services.")
    subparsers.add_parser("reset", help="Resets the environment.").add_argument("--hard", action="store_true")

    subparsers.add_parser("tui", help="Runs the TUI.")
    subparsers.add_parser("install-plugin", help="Installs a plugin.").add_argument("name")

    args = parser.parse_args()

    if args.command == "setup":
        setup(args)
    elif args.command == "stop":
        stop()
    elif args.command == "reset":
        reset(args.hard)
    elif args.command == "run":
        run(args)
    elif args.command == "install":
        install(args)
    elif args.command == "tui":
        try:
            from link.ui.tui import start_tui

            start_tui()
        except ImportError:
            logger.error("TUI missing.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
