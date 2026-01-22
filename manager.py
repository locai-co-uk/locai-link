# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import argparse
import os
import platform
import shutil
import signal
import subprocess
import sys
from pathlib import Path

# --- Constants ---
PROJECT_ROOT = Path(__file__).parent.resolve()
VENV_NAME = ".venv"
VENV_PATH = PROJECT_ROOT / VENV_NAME
AGENT_SCRIPT = PROJECT_ROOT / "src" / "link" / "agent.py"
PYTHON_VERSION = os.environ.get("PYTHON_VERSION", "3.11")

# --- Helper Functions ---


def print_step(message):
    """Print a formatted step message.

    Args:
        message (str): The message to print.
    """
    print(f"\n=== {message} ===")


def signal_handler(signum, frame):
    """Handles termination signals by initiating a clean exit.

    Args:
        signum (int): The signal number.
        frame (frame): The current stack frame.
    """
    print(f"\nReceived signal {signum}. Exiting gracefully...")
    sys.exit(0)


# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def is_uv_installed() -> bool:
    """Check if uv is installed and accessible.

    Returns:
        bool: True if uv is installed, False otherwise.
    """
    try:
        subprocess.run(
            ["uv", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def install_uv():
    """Install uv using the appropriate method for the OS."""
    print_step("Installing uv (package manager)")

    system = platform.system().lower()

    try:
        if system == "windows":
            # Use PowerShell to install on Windows
            cmd = 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
            subprocess.run(cmd, shell=True, check=True)
        else:
            # Use curl/sh on Linux/macOS
            cmd = "curl -LsSf https://astral.sh/uv/install.sh | sh"
            subprocess.run(cmd, shell=True, check=True)

        # Add typical install locations to PATH for this session
        home = Path.home()
        if system == "windows":
            # Typical windows location
            uv_path = home / ".local" / "bin"  # generic
            if not uv_path.exists():
                uv_path = home / ".cargo" / "bin"  # fallback
        else:
            uv_path = home / ".local" / "bin"

        if uv_path.exists():
            os.environ["PATH"] += os.pathsep + str(uv_path)
            print(f"✔ Added {uv_path} to PATH for this session")

    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to install uv: {e}")
        sys.exit(1)


def get_venv_python() -> Path:
    """Returns the path to the python executable inside the venv.

    Returns:
        Path: The path to the python executable inside the venv.
    """
    if sys.platform == "win32":
        return VENV_PATH / "Scripts" / "python.exe"
    else:
        return VENV_PATH / "bin" / "python"


def ensure_venv_execution():
    """Check if we are running inside the virtual environment."""
    # Check if we are already in a venv (sys.prefix != sys.base_prefix)
    if sys.prefix != sys.base_prefix:
        return

    venv_python = get_venv_python()

    if venv_python.exists():
        # We found the venv, but we aren't using it. Re-exec.
        # print(f"🔄 Switching to virtual environment: {venv_python}")

        # Construct the command: [path/to/venv/python, manager.py, arg1, arg2...]
        args = [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:]

        try:
            # On Unix, os.execv replaces the current process
            if sys.platform != "win32":
                os.execv(str(venv_python), args)
            else:
                # On Windows, use subprocess and exit
                subprocess.run(args, check=True)
                sys.exit(0)
        except OSError as e:
            print(f"❌ Failed to switch to venv: {e}")
            sys.exit(1)
    else:
        # If running 'setup', we don't expect the venv to exist yet.
        # For other commands, we warn.
        if "setup" not in sys.argv:
            print("⚠ Warning: .venv not found. Run 'python manager.py setup' first.")


# --- Command Implementations ---


def setup(extras=None):
    """Sets up the environment: installs uv, creates venv, installs dependencies, handles .env.

    Args:
        extras (str, optional): Extra dependencies to install. Defaults to None.
    """
    print_step(f"Setting up LocAI Device Environment (Python {PYTHON_VERSION})")

    # 1. Check/Install uv
    if not is_uv_installed():
        install_uv()
    else:
        print("✔ uv is already installed")

    # 2. Create Virtual Environment using uv if it doesn't exist
    print_step("Creating Virtual Environment")
    if VENV_PATH.exists():
        print(f"✔ Virtual environment already exists at {VENV_PATH}")
    else:
        try:
            subprocess.run(
                ["uv", "venv", "--python", PYTHON_VERSION, ".venv"],
                cwd=PROJECT_ROOT,
                check=True,
            )
            print(f"✔ Virtual environment created at {VENV_PATH}")
        except subprocess.CalledProcessError:
            print("❌ Failed to create virtual environment.")
            sys.exit(1)

    # 3. Install Dependencies using uv pip
    print_step("Installing Dependencies")

    install_target = "-e ."
    if extras:
        install_target = f"-e .[{extras}]"
        print(f"  -> Including extras: {extras}")
    else:
        print("  -> Installing CORE dependencies.")

    try:
        # Determine environment for uv pip
        env = os.environ.copy()
        env["VIRTUAL_ENV"] = str(VENV_PATH)

        subprocess.run(
            ["uv", "pip", "install"] + install_target.split(),
            cwd=PROJECT_ROOT,
            env=env,
            check=True,
        )
        print("✔ Dependencies installed successfully.")
    except subprocess.CalledProcessError:
        print("❌ Failed to install dependencies.")
        sys.exit(1)

    # 4. Setup Default .env if missing
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        print_step("Checking Environment Configuration")
        example_env = PROJECT_ROOT / ".env.example"
        if example_env.exists():
            shutil.copy(example_env, env_path)
            print("✔ Copied .env.example to .env")
        else:
            print("⚠ No .env.example found, skipping .env creation.")

    print("\nSUCCESS! Environment ready.")


def reset(hard=False):
    """Cleans up the environment.

    Args:
        hard (bool, optional): Whether to remove .env and agent_config.json.
            Defaults to False.
    """
    print_step("Resetting device environment")

    # Patterns to remove. Using glob syntax allowing wildcards.
    patterns_to_remove = [
        VENV_NAME,
        "*.egg-info",
        "build",
        "dist",
        "__pycache__",
        "uv.lock",
    ]

    # 1. Remove Directories matching patterns
    for pattern in patterns_to_remove:
        # PROJECT_ROOT.glob(pattern) handles both exact names (.venv) and wildcards (*.egg-info)
        for path in PROJECT_ROOT.glob(pattern):
            if path.is_dir():
                print(f"Removing directory: {path.name}...")
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file():
                print(f"Removing file: {path.name}...")
                path.unlink(missing_ok=True)

    # 2. Remove __pycache__ recursively from subdirectories
    # (The loop above only catches __pycache__ in the root)
    print("Scanning for nested __pycache__...")
    for p in PROJECT_ROOT.rglob("__pycache__"):
        if p.is_dir():
            # Avoid printing every single nested cache to keep logs clean
            shutil.rmtree(p, ignore_errors=True)

    # 3. Remove .env if hard reset
    if hard:
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            print("Removing .env...")
            env_path.unlink()

        config_path = PROJECT_ROOT / "agent_config.json"
        if config_path.exists():
            print("Removing agent_config.json...")
            config_path.unlink()

    print("Reset complete.")


def run_agent_process(agent_args):
    """Runs the agent.py script.

    Assumes we are already in the venv (handled by ensure_venv_execution).

    Args:
        agent_args (list): List of arguments to pass to agent.py.
    """
    cmd = [sys.executable, str(AGENT_SCRIPT)] + agent_args

    print(f"🚀 Executing agent command: {' '.join(cmd)}")

    try:
        # Replace the current process with the agent process
        if sys.platform != "win32":
            os.execv(sys.executable, cmd)
        else:
            # Windows doesn't support execv well, so we subprocess
            subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        sys.exit(130)


def main():
    """Main entry point for the manager script."""
    parser = argparse.ArgumentParser(description="LocAI Device Manager")
    subparsers = parser.add_subparsers(
        dest="command", required=True, help="Command to run"
    )

    # --- SETUP COMMAND ---
    setup_parser = subparsers.add_parser(
        "setup", help="Install uv, create venv, install deps"
    )
    setup_parser.add_argument(
        "--extras", default="", help="Comma-separated optional profiles (e.g. 'dev')"
    )

    # --- RESET COMMAND ---
    reset_parser = subparsers.add_parser("reset", help="Clean up artifacts")
    reset_parser.add_argument(
        "--hard", action="store_true", help="Also remove .env and config files"
    )

    # --- REGISTER COMMAND ---
    reg_parser = subparsers.add_parser("register", help="Register a new device")
    reg_parser.add_argument(
        "--device-name", required=True, help="Name for the new device"
    )
    reg_parser.add_argument("--username", required=True, help="Platform username")
    reg_parser.add_argument(
        "--registration-key", required=True, help="Registration key"
    )
    reg_parser.add_argument(
        "--device-type",
        default="edge_device",
        help="Device type (default: edge_device)",
    )
    reg_parser.add_argument("--api-url", help="Override API URL")

    # --- ACTIVATE COMMAND ---
    act_parser = subparsers.add_parser(
        "activate", help="Activate a pre-registered device"
    )
    act_parser.add_argument("--device-id", required=True, help="Device ID")
    act_parser.add_argument("--api-key", help="API Key (if activated in UI)")
    act_parser.add_argument(
        "--registration-key", help="Registration Key (if activating via terminal)"
    )
    act_parser.add_argument("--device-type", help="Device type (optional)")
    act_parser.add_argument("--api-url", help="Override API URL")

    # --- RUN COMMAND ---
    run_parser = subparsers.add_parser("run", help="Run the agent")
    run_parser.add_argument("--api-url", help="Override API URL")

    args = parser.parse_args()

    # Dispatch commands
    if args.command == "setup":
        setup(extras=args.extras)
        return

    if args.command == "reset":
        reset(hard=args.hard)
        return

    # --- AUTO-SETUP CHECK ---
    # If the virtual environment (or python executable) is missing, run setup automatically.
    if not VENV_PATH.exists() or not get_venv_python().exists():
        print_step("Environment Not Found")
        print("⚠ The virtual environment is not set up yet.")
        print("  initiating automatic setup (Core Dependencies)...")

        setup()  # Run the setup function defined above

        # Verify setup actually worked before proceeding
        if not get_venv_python().exists():
            print(
                "❌ Automatic setup failed. Please run 'python manager.py setup' manually to debug."
            )
            sys.exit(1)

    # For register, activate, and run, we MUST be in the venv.
    # Now that we've run auto-setup, this will successfully switch contexts.
    ensure_venv_execution()

    # Construct arguments for agent.py
    agent_cmd_args = []

    if hasattr(args, "api_url") and args.api_url:
        agent_cmd_args.extend(["--api-url", args.api_url])

    # Device type not yet implemented
    if args.device_type and args.device_type != "edge_device":
        print(
            f"⚠ Warning: --device-type '{args.device_type}' is currently not implemented."
        )
        print("  Proceeding with standard behavior.")

    if args.command == "register":
        agent_cmd_args.extend(
            [
                "--device-name",
                args.device_name,
                "--username",
                args.username,
                "--registration-key",
                args.registration_key,
                "--device-type",
                args.device_type,
            ]
        )

    elif args.command == "activate":
        agent_cmd_args.extend(["--device-id", args.device_id])
        if args.api_key:
            agent_cmd_args.extend(["--api-key", args.api_key])
        elif args.registration_key:
            agent_cmd_args.extend(["--registration-key", args.registration_key])
        else:
            print("Error: activate requires either --api-key or --registration-key")
            sys.exit(1)

        if args.device_type:
            agent_cmd_args.extend(["--device-type", args.device_type])

    elif args.command == "run":
        pass  # No specific args needed for standard run

    # Execute agent
    run_agent_process(agent_cmd_args)


if __name__ == "__main__":
    main()
