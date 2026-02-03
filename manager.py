# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from link.server import ModelServer

# --- Constants ---
PROJECT_ROOT = Path(__file__).parent.resolve()
VENV_NAME = ".venv"
VENV_PATH = PROJECT_ROOT / VENV_NAME
PYTHON_VERSION = os.environ.get("PYTHON_VERSION", "3.11")
AGENT_SCRIPT = PROJECT_ROOT / "src" / "link" / "agent.py"
CONFIG_FILE = PROJECT_ROOT / "configs" / "agent_config.json"

# Installer Defaults
DEFAULT_REPO_URL = "https://github.com/locai-co-uk/locai-link.git"
DEFAULT_BRANCH = "main"

# API Environments
PROD_API_URL = "https://api.locai.co.uk/api/v1"

# --- Infrastructure Helpers ---


def print_step(message):
    """Prints a step header."""
    print(f"\n=== {message} ===")


def command_exists(cmd: str) -> bool:
    """Checks if a command exists in the system's PATH."""
    return shutil.which(cmd) is not None


def is_uv_installed() -> bool:
    """Checks if uv (The Package Manager) is installed."""
    try:
        subprocess.run(["uv", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def install_uv():
    """Installs uv (The Package Manager) if missing."""
    if is_uv_installed():
        return

    print_step("Installing uv (package manager)")
    system = platform.system().lower()

    try:
        if system == "windows":
            cmd = 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
            subprocess.run(cmd, shell=True, check=True)
        else:
            cmd = "curl -LsSf https://astral.sh/uv/install.sh | sh"
            subprocess.run(cmd, shell=True, check=True)

        # Attempt to add to PATH for current session
        home = Path.home()
        new_paths = [home / ".local" / "bin", home / ".cargo" / "bin"]
        for p in new_paths:
            if p.exists():
                os.environ["PATH"] += os.pathsep + str(p)
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to install uv: {e}")
        sys.exit(1)


def ensure_venv_execution():
    """Ensures the script is running inside the project's virtual environment."""
    if sys.prefix != sys.base_prefix:
        return

    # Determine venv python path
    if sys.platform == "win32":
        venv_python = VENV_PATH / "Scripts" / "python.exe"
    else:
        venv_python = VENV_PATH / "bin" / "python"

    if venv_python.exists():
        # Re-execute the current command inside the venv
        args = [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:]
        try:
            if sys.platform != "win32":
                os.execv(str(venv_python), args)
            else:
                subprocess.run(args, check=True)
                sys.exit(0)
        except OSError as e:
            print(f"❌ Failed to switch to venv: {e}")
            sys.exit(1)
    else:
        print("❌ Virtual environment not found.")
        print("Please run: python manager.py setup")
        sys.exit(1)


def install_deps_from_source():  # Can be repurposed to install all components that require building from source
    """Installs packages that cannot be installed directly with pip/setuptools."""
    print_step("Installing AI Inference Engine")
    system = platform.system()
    machine = platform.machine()

    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(VENV_PATH)

    def uv_pip_install(args, extra_env=None):
        run_env = env.copy()
        if extra_env:
            run_env.update(extra_env)
        subprocess.run(["uv", "pip", "install"] + args, cwd=PROJECT_ROOT, env=run_env, check=True)

    try:
        if system == "Darwin":
            print(f"Detected macOS ({machine}). Building with Metal support...")
            if not command_exists("cmake"):
                uv_pip_install(["cmake"])

            cmake_args = "-DGGML_METAL=ON -DGGML_NATIVE=OFF"
            if machine == "arm64":
                cmake_args += " -DCMAKE_OSX_ARCHITECTURES=arm64"

            uv_pip_install(
                ["--no-binary", "llama-cpp-python", "llama-cpp-python"],
                extra_env={"FORCE_CMAKE": "1", "CMAKE_ARGS": cmake_args},
            )

        elif system == "Linux":
            print("Detected Linux.")
            if not command_exists("cmake"):
                uv_pip_install(["cmake"])

            has_gpu = command_exists("nvidia-smi") and command_exists("nvcc")
            cmake_args = "-DGGML_CUDA=ON" if has_gpu else ""
            print("✔ CUDA detected." if has_gpu else "No CUDA detected. Using CPU.")

            uv_pip_install(
                ["--no-binary", "llama-cpp-python", "llama-cpp-python"],
                extra_env={"FORCE_CMAKE": "1", "CMAKE_ARGS": cmake_args},
            )

        elif system == "Windows":
            print("Detected Windows.")
            has_gpu = command_exists("nvidia-smi") and command_exists("nvcc")
            # Use pre-built wheels for Windows to avoid complex build tools
            index_url = (
                "https://abetlen.github.io/llama-cpp-python/whl/cu121"
                if has_gpu
                else "https://abetlen.github.io/llama-cpp-python/whl/cpu"
            )
            print("✔ GPU detected." if has_gpu else "No GPU detected or No CUDA detected. Using CPU.")

            uv_pip_install(["llama-cpp-python", "--extra-index-url", index_url])

        else:
            uv_pip_install(["llama-cpp-python"])

        print("✔ Inference Engine installed.")
    except subprocess.CalledProcessError:
        print("❌ Failed to install Inference Engine.")
        sys.exit(1)


# --- Main Commands ---


def setup(extras=None):
    """Builder: Prepares the environment."""
    print_step(f"Setting up Environment (Python {PYTHON_VERSION})")

    install_uv()

    if VENV_PATH.exists():
        print(f"✔ Virtual environment exists at {VENV_PATH}")
    else:
        print("Creating virtual environment...")
        subprocess.run(["uv", "venv", "--python", PYTHON_VERSION, ".venv"], cwd=PROJECT_ROOT, check=True)

    install_deps_from_source()

    print_step("Installing Project Dependencies")
    install_target = f"-e .[{extras}]" if extras else "-e ."

    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(VENV_PATH)
    subprocess.run(["uv", "pip", "install"] + install_target.split(), cwd=PROJECT_ROOT, env=env, check=True)

    print("\n✔ Setup Complete.")


def install(args):
    """Orchestrator: The 'Web Installer' Logic."""
    print_step("LocAI Edge Agent Installer")
    cwd = Path.cwd()

    # Determine API URL
    target_api_url = PROD_API_URL  # Default

    if args.api_url:
        target_api_url = args.api_url
        print(f"✔ Using provided API URL: {target_api_url}")
    elif args.dev:
        print("\n--- Development Configuration ---")
        # Flush input buffer to prevent skipping
        try:
            sys.stdin.flush()
        except Exception:
            pass

        user_input = input("Enter Target API URL: ").strip()
        if not user_input:
            print("❌ Error: API URL is required when using --dev.")
            sys.exit(1)
        target_api_url = user_input
        print(f"✔ Selected Custom URL: {target_api_url}")

    # Git Operations
    if (cwd / "pyproject.toml").exists():
        install_dir = cwd
        print(f"✔ Detected existing repository in {install_dir}")
        is_fresh_clone = False
    else:
        install_dir = cwd / "locai-link"
        print(f"Target Directory: {install_dir}")
        is_fresh_clone = True

    if not command_exists("git"):
        print("❌ Error: git is not installed.")
        sys.exit(1)

    if is_fresh_clone:
        if install_dir.exists():
            print("Updating existing directory...")
            subprocess.run(["git", "pull", "origin", args.branch], cwd=install_dir, check=True)
        else:
            print(f"Cloning repository ({args.branch})...")
            subprocess.run(
                ["git", "clone", "--depth", "1", "-b", args.branch, args.repo_url, str(install_dir)], check=True
            )
    else:
        subprocess.run(["git", "pull", "origin", args.branch], cwd=install_dir, check=True)

    # Handover to Local Manager
    print_step("Handing over to local installer...")

    local_manager = install_dir / "manager.py"
    if not local_manager.exists():
        print("❌ Error: manager.py not found in target directory.")
        sys.exit(1)

    # Interactive Inputs (only if not provided)
    if not args.device_name:
        args.device_name = input("Enter Device Name: ").strip()
    if not args.username:
        args.username = input("Enter Username: ").strip()
    if not args.registration_key:
        args.registration_key = input("Enter Registration Key: ").strip()

    if not all([args.device_name, args.username, args.registration_key]):
        print("❌ Error: All fields are required.")
        sys.exit(1)

    # Define helper to run commands inside the new repo
    def run_target(cmd_list):
        full_cmd = ["uv", "run", "manager.py"] + cmd_list
        subprocess.run(full_cmd, cwd=install_dir, check=True)

    try:
        # A. Setup
        run_target(["setup"])

        # B. Register
        reg_args = [
            "register",
            "--device-name",
            args.device_name,
            "--username",
            args.username,
            "--registration-key",
            args.registration_key,
            "--device-type",
            args.device_type,
            "--api-url",
            target_api_url,
        ]
        run_target(reg_args)

        # C. Run
        start = args.start_running
        if not start and sys.stdin.isatty():
            confirm = input("\nDo you want to start the agent now? [Y/n] ").strip().lower()
            if confirm in ["", "y", "yes"]:
                start = True

        if start:
            run_target(["run", "--api-url", target_api_url])
        else:
            print(f"\n✔ Installation complete. To run later:\n  cd {install_dir}\n  uv run manager.py run")

    except subprocess.CalledProcessError as e:
        print(f"\n❌ Installation step failed (Exit Code: {e.returncode})")
        sys.exit(e.returncode)


def reset(hard=False):
    """Cleans up the environment."""
    print_step("Resetting device environment")

    # Patterns to remove
    patterns_to_remove = [
        VENV_NAME,
        "*.egg-info",
        "build",
        "dist",
        "__pycache__",
        ".benchmarks",
        "uv.lock",
        ".ruff_cache",
        ".pytest_cache",
        ".coverage",
        "serving.pid",
        "serving.log",
    ]

    for pattern in patterns_to_remove:
        for path in PROJECT_ROOT.rglob(pattern):
            if path == PROJECT_ROOT:
                continue

            if path.is_dir():
                print(f"Removing directory: {path.relative_to(PROJECT_ROOT)}...")
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file():
                print(f"Removing file: {path.relative_to(PROJECT_ROOT)}...")
                path.unlink(missing_ok=True)

    if hard:
        config_dir = PROJECT_ROOT / "configs"
        if config_dir.exists():
            print(f"Clearing all files in {config_dir.name}...")
            for item in config_dir.iterdir():
                if item.is_file() and item.name != ".gitkeep":
                    print(f"Removing config file: {item.name}")
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)

    print("Reset complete.")


def start_serving():
    """Starts the serving process using the Server module."""
    server = ModelServer()
    server.start()


def stop_serving():
    """Stops the serving process using the Server module."""
    server = ModelServer()
    server.stop()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="LocAI Device Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. Install
    install_parser = subparsers.add_parser("install", help="Full installation wizard")
    install_parser.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    install_parser.add_argument("--branch", default=DEFAULT_BRANCH)
    install_parser.add_argument("--device-name")
    install_parser.add_argument("--username")
    install_parser.add_argument("--registration-key")
    install_parser.add_argument("--device-type", default="edge_device")
    install_parser.add_argument("--start-running", action="store_true")
    install_parser.add_argument("--api-url", help="Specific API URL override")
    install_parser.add_argument("--dev", action="store_true", help="Prompt for custom API URL")

    # 2. Setup
    setup_parser = subparsers.add_parser("setup", help="Configure venv and deps")
    setup_parser.add_argument("--extras", default="")

    # 3. Reset
    reset_parser = subparsers.add_parser("reset", help="Clean up artifacts")
    reset_parser.add_argument("--hard", action="store_true", help="Also remove config files")

    # 4. Serving Commands
    subparsers.add_parser("start-serving", help="Start the serving process")
    subparsers.add_parser("stop-serving", help="Stop the serving process")

    # 5. Register
    reg_parser = subparsers.add_parser("register", help="Register device")
    reg_parser.add_argument("--device-name")
    reg_parser.add_argument("--username")
    reg_parser.add_argument("--registration-key")
    reg_parser.add_argument("--device-type", default="edge_device")
    reg_parser.add_argument("--api-url")

    # 6. Activate (Restored)
    act_parser = subparsers.add_parser("activate", help="Activate a pre-registered device")
    act_parser.add_argument("--device-id", required=True, help="Device ID")
    act_parser.add_argument("--api-key", help="API Key (if activated in UI)")
    act_parser.add_argument("--registration-key", help="Registration Key (if activating via terminal)")
    act_parser.add_argument("--device-type", help="Device type (optional)")
    act_parser.add_argument("--api-url", help="Override API URL")

    # 7. Run
    run_parser = subparsers.add_parser("run", help="Run agent")
    run_parser.add_argument("--api-url")

    args = parser.parse_args()

    # --- DISPATCHER ---

    # Commands that do NOT require the Virtual Env
    if args.command == "install":
        install(args)
        return

    elif args.command == "setup":
        setup(extras=args.extras)
        return

    elif args.command == "reset":
        reset(hard=args.hard)
        return

    # Commands that REQUIRE the Virtual Env
    ensure_venv_execution()

    if args.command == "start-serving":
        start_serving()
        return

    elif args.command == "stop-serving":
        stop_serving()
        return

    if args.command == "register":
        if not all([args.device_name, args.username, args.registration_key]):
            print("❌ Error: Missing required arguments (name, username, key).")
            sys.exit(1)

        cmd = [
            sys.executable,
            str(AGENT_SCRIPT),
            "--device-name",
            args.device_name,
            "--username",
            args.username,
            "--registration-key",
            args.registration_key,
            "--device-type",
            args.device_type,
            "--api-url",
            args.api_url if args.api_url else PROD_API_URL,
        ]
        subprocess.run(cmd, check=True)

    elif args.command == "activate":
        # Logic restored from old main
        cmd = [sys.executable, str(AGENT_SCRIPT), "--device-id", args.device_id]

        if args.api_key:
            cmd.extend(["--api-key", args.api_key])
        elif args.registration_key:
            cmd.extend(["--registration-key", args.registration_key])
        else:
            print("❌ Error: activate requires either --api-key or --registration-key")
            sys.exit(1)

        if args.device_type:
            cmd.extend(["--device-type", args.device_type])

        cmd.extend(["--api-url", args.api_url if args.api_url else PROD_API_URL])
        subprocess.run(cmd, check=True)

    elif args.command == "run":
        if not CONFIG_FILE.exists():
            print("❌ Error: Device not configured. Run 'register' first.")
            sys.exit(1)

        # Only append --api-url if it was actually provided in the CLI
        cmd = [sys.executable, str(AGENT_SCRIPT)]
        if args.api_url:
            cmd.extend(["--api-url", args.api_url])

        subprocess.run(cmd, check=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(0)
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Command execution failed (Exit Code: {e.returncode})")

        cmd_str = str(e.cmd)
        if "agent.py" in cmd_str:
            print("   The agent process crashed. Please check the logs above for details.")
        elif "git" in cmd_str:
            print("   Git operation failed. Check your internet connection or permissions.")
        elif "uv" in cmd_str:
            print("   Dependency installation failed.")

        sys.exit(e.returncode)
    except Exception as e:
        print(f"\n❌ An unexpected error occurred: {e}")
        raise e
        sys.exit(1)
