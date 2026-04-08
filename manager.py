# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

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


def install_deps_from_source():
    """Downloads and installs the official llama.cpp binaries from GitHub Releases."""
    print_step("Installing AI Inference Engine")

    # Ensure bin directory exists
    bin_dir = VENV_PATH / "bin-llama"
    bin_dir.mkdir(parents=True, exist_ok=True)

    system = platform.system()
    machine = platform.machine()

    # 1. Determine Asset
    asset_keyword = ""
    expected_ext = ""

    if system == "Darwin":
        expected_ext = ".tar.gz"
        if machine == "arm64":
            asset_keyword = "macos-arm64"
        else:
            asset_keyword = "macos-x64"

    elif system == "Windows":
        expected_ext = ".zip"
        has_gpu = False
        if command_exists("nvidia-smi"):
            try:
                res = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True)
                if res.returncode == 0 and len(res.stdout.strip()) > 0:
                    has_gpu = True
            except Exception:
                pass

        if has_gpu:
            print("NVIDIA GPU detected.")
            asset_keyword = "bin-win-cuda-12"
        else:
            print("No GPU detected. Using CPU build.")
            asset_keyword = "bin-win-cpu-x64"

    elif system == "Linux":
        expected_ext = ".tar.gz"

        # Check for NVIDIA GPU
        if command_exists("nvidia-smi"):
            print("NVIDIA GPU detected.")
            print("(Official CUDA binaries are not distributed for Linux due to driver compatibility)")
            print("Switched target to **Vulkan** build (supports NVIDIA GPUs).")
            asset_keyword = "bin-ubuntu-vulkan-x64"
        else:
            print("No GPU detected. Using CPU build.")
            asset_keyword = "bin-ubuntu-x64"

    else:
        print(f"❌ Unsupported platform: {system} {machine}")
        sys.exit(1)

    print(f"Targeting Release Asset: *{asset_keyword}*")

    # 2. Fetch Release Info
    try:
        print("Fetching latest release info...")
        gh_token = os.environ.get("GITHUB_TOKEN")
        api_headers = {"Accept": "application/vnd.github+json"}
        if gh_token:
            api_headers["Authorization"] = f"Bearer {gh_token}"
        api_req = urllib.request.Request(
            "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest",
            headers=api_headers,
        )
        with urllib.request.urlopen(api_req) as response:
            release_data = json.loads(response.read().decode())

        assets = release_data.get("assets", [])
        download_url = None
        asset_name = None

        for asset in assets:
            name = asset["name"]
            if asset_keyword in name and name.endswith(expected_ext) and name.startswith("llama-"):
                download_url = asset["browser_download_url"]
                asset_name = name
                break

        if not download_url:
            print(f"❌ Could not find a suitable binary for {asset_keyword}")
            # Fallback for Linux: If Vulkan is missing, try standard CPU
            if "vulkan" in asset_keyword:
                print("   Falling back to standard CPU build...")
                fallback_keyword = "bin-ubuntu-x64"
                for asset in assets:
                    name = asset["name"]
                    if fallback_keyword in name and name.endswith(expected_ext):
                        download_url = asset["browser_download_url"]
                        asset_name = name
                        break

            if not download_url:
                sys.exit(1)

        # 3. Download
        dest_file = bin_dir / asset_name
        print(f"Downloading {asset_name}...")
        urllib.request.urlretrieve(download_url, dest_file)

        # 4. Extract
        print("Extracting...")
        if dest_file.suffix == ".zip":
            with zipfile.ZipFile(dest_file, "r") as zip_ref:
                zip_ref.extractall(bin_dir)

        elif dest_file.name.endswith(".tar.gz") or dest_file.suffix == ".tar":
            with tarfile.open(dest_file, "r:*") as tar_ref:
                tar_ref.extractall(bin_dir)

        os.remove(dest_file)

        # 5. Flatten & Cleanup (Critical for finding libs!)
        print("Flattening directory structure...")
        # Move EVERYTHING from subfolders to BIN_DIR root
        for root, dirs, files in os.walk(bin_dir):
            if Path(root) == bin_dir:
                continue

            for file in files:
                src = Path(root) / file
                dst = bin_dir / file
                if not dst.exists():
                    shutil.move(src, dst)

        # Cleanup empty folders
        for child in bin_dir.iterdir():
            if child.is_dir() and child.name.startswith("llama-"):
                try:
                    shutil.rmtree(child)
                except OSError:
                    pass

        # 6. Permissions
        if system != "Windows":
            print("Setting permissions...")
            for f in bin_dir.glob("llama-*"):
                f.chmod(0o755)

        print(f"Inference Engine installed successfully in {bin_dir}")

    except Exception as e:
        print(f"❌ Failed to install Inference Engine: {e}")
        sys.exit(1)


# --- Main Commands ---


def setup(extras=None):
    """Builder: Prepares the environment."""
    print_step(f"Setting up Environment (Python {PYTHON_VERSION})")

    install_uv()

    if VENV_PATH.exists():
        print(f"Virtual environment exists at {VENV_PATH}")
    else:
        print("Creating virtual environment...")
        subprocess.run(["uv", "venv", "--python", PYTHON_VERSION, ".venv"], cwd=PROJECT_ROOT, check=True)

    install_deps_from_source()

    print_step("Installing Project Dependencies")
    install_target = f"-e .[{extras}]" if extras else "-e ."

    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(VENV_PATH)
    subprocess.run(["uv", "pip", "install"] + install_target.split(), cwd=PROJECT_ROOT, env=env, check=True)

    print("\nSetup Complete.")


def get_local_version() -> str | None:
    """Reads the version string from pyproject.toml."""
    toml_path = PROJECT_ROOT / "pyproject.toml"
    if not toml_path.exists():
        return None
    for line in toml_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            parts = line.split("=", 1)
            if len(parts) == 2:
                return parts[1].strip().strip('"').strip("'")
    return None


def update(repo_dir: Path, branch: str = DEFAULT_BRANCH) -> bool:
    """Pulls the latest code from the remote, stashing any local changes.

    Returns True if the codebase was updated, False if already up to date.
    """
    print_step("Checking for Updates")

    if not command_exists("git"):
        print("❌ git is not installed — cannot check for updates.")
        return False

    # Fetch without merging so we can compare first
    subprocess.run(["git", "fetch", "origin", branch], cwd=repo_dir, check=True)

    # Count commits the local branch is behind the remote
    result = subprocess.run(
        ["git", "rev-list", "--count", f"HEAD..origin/{branch}"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    behind = int(result.stdout.strip() or "0")

    if behind == 0:
        local_ver = get_local_version()
        print(f"Already up to date{f' (v{local_ver})' if local_ver else ''}.")
        return False

    print(f"Update available: {behind} new commit(s) on {branch}.")

    # Check for local modifications that would block the pull
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    ).stdout.strip()

    stashed = False
    if dirty:
        print("Local modifications detected — stashing before update...")
        stash_result = subprocess.run(
            ["git", "stash", "push", "--include-untracked", "-m", "locai-auto-stash"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if stash_result.returncode != 0:
            print("❌ Could not stash local changes. Aborting update to avoid data loss.")
            print("   Resolve conflicts manually, then run: uv run manager.py update")
            return False
        stashed = True

    # Pull
    subprocess.run(["git", "pull", "origin", branch], cwd=repo_dir, check=True)

    # Restore stash if we created one
    if stashed:
        pop_result = subprocess.run(
            ["git", "stash", "pop"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if pop_result.returncode != 0:
            print("⚠️  Update succeeded but stash could not be re-applied cleanly.")
            print("   Your changes are saved in git stash — run 'git stash show' to review.")
        else:
            print("Local changes re-applied successfully.")

    # Re-install dependencies in case pyproject.toml changed
    print("Updating dependencies...")
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(VENV_PATH)
    subprocess.run(["uv", "pip", "install", "-e", "."], cwd=repo_dir, env=env, check=True)

    new_ver = get_local_version()
    print(f"✅ Update complete{f' — now at v{new_ver}' if new_ver else ''}.")
    return True


def install(args):
    """Orchestrator: The 'Web Installer' Logic."""
    print_step("Loc.ai Agent Installer")
    cwd = Path.cwd()

    # Determine API URL
    target_api_url = PROD_API_URL  # Default

    if args.api_url:
        target_api_url = args.api_url
        print(f"Using provided API URL: {target_api_url}")
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
        print(f"Selected Custom URL: {target_api_url}")

    # Git Operations
    if (cwd / "pyproject.toml").exists():
        install_dir = cwd
        print(f"Detected existing repository in {install_dir}")
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
            print("Existing installation found — checking for updates...")
            update(install_dir, args.branch)
        else:
            print(f"Cloning repository ({args.branch})...")
            subprocess.run(
                ["git", "clone", "--depth", "1", "-b", args.branch, args.repo_url, str(install_dir)], check=True
            )
    else:
        print("Running from repository — checking for updates...")
        update(install_dir, args.branch)

    # Handover to Local Manager
    print_step("Handing over to local installer...")

    local_manager = install_dir / "manager.py"
    if not local_manager.exists():
        print("❌ Error: manager.py not found in target directory.")
        sys.exit(1)

    # Interactive Inputs (only if not provided)
    if not args.device_name:
        args.device_name = input("Enter Device Name: ").strip()
    if not args.token and not args.email:
        args.email = input("Enter Email: ").strip()
    if not args.registration_key:
        args.registration_key = input("Enter Registration Key: ").strip()

    identity_provided = args.token or args.email
    if not all([args.device_name, args.registration_key]) or not identity_provided:
        print("❌ Error: Device name, registration key, and an identity (--email or --token) are required.")
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
            "--registration-key",
            args.registration_key,
            "--device-type",
            args.device_type,
            "--api-url",
            target_api_url,
        ]
        if args.token:
            reg_args += ["--token", args.token]
        else:
            reg_args += ["--email", args.email]
            # Do NOT pass --password here; agent.py will prompt securely via getpass
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
            print(f"\nInstallation complete. To run later:\n  cd {install_dir}\n  uv run manager.py run")

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

            if pattern == VENV_NAME:
                try:
                    if Path(sys.prefix).resolve() == path.resolve():
                        print(f"Skipping active virtual environment: {path.name} (Cannot delete while in use)")
                        print("(To fully reset, exit the process/uv and delete '.venv' manually)")
                        continue
                except Exception:
                    pass

            if path.is_dir():
                print(f"Removing directory: {path.relative_to(PROJECT_ROOT)}...")
                try:
                    shutil.rmtree(path, ignore_errors=True)
                except Exception as e:
                    print(f"Warning: Failed to remove {path.name}: {e}")
            elif path.is_file():
                print(f"Removing file: {path.relative_to(PROJECT_ROOT)}...")
                try:
                    path.unlink(missing_ok=True)
                except Exception as e:
                    print(f"Warning: Failed to remove {path.name}: {e}")

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


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="LocAI Device Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. Install
    install_parser = subparsers.add_parser("install", help="Full installation wizard")
    install_parser.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    install_parser.add_argument("--branch", default=DEFAULT_BRANCH)
    install_parser.add_argument("--device-name")
    install_parser.add_argument("--email")
    install_parser.add_argument("--password")
    install_parser.add_argument("--token", help="Pre-obtained JWT access token (alternative to email/password)")
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

    # 4. Register
    reg_parser = subparsers.add_parser("register", help="Register device")
    reg_parser.add_argument("--device-name")
    reg_parser.add_argument("--email")
    reg_parser.add_argument("--password")
    reg_parser.add_argument("--token", help="Pre-obtained JWT access token (alternative to email/password)")
    reg_parser.add_argument("--registration-key")
    reg_parser.add_argument("--device-type", default="other")
    reg_parser.add_argument("--api-url")

    # 5. Activate (Restored)
    act_parser = subparsers.add_parser("activate", help="Activate a pre-registered device")
    act_parser.add_argument("--device-id", required=True, help="Device ID")
    act_parser.add_argument("--api-key", help="API Key (if activated in UI)")
    act_parser.add_argument("--registration-key", help="Registration Key (if activating via terminal)")
    act_parser.add_argument("--device-type", help="Device type (optional)")
    act_parser.add_argument("--api-url", help="Override API URL")

    # 6. Install Deps (binary only, no venv creation)
    subparsers.add_parser("install-deps", help="Download llama.cpp server binary")

    # 7. Update
    update_parser = subparsers.add_parser("update", help="Pull latest code and update dependencies")
    update_parser.add_argument("--branch", default=DEFAULT_BRANCH)

    # 8. Run
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

    elif args.command == "install-deps":
        install_deps_from_source()
        return

    elif args.command == "update":
        update(PROJECT_ROOT, args.branch)
        return

    # Commands that REQUIRE the Virtual Env
    ensure_venv_execution()

    if args.command == "register":
        identity_provided = args.token or args.email
        if not args.device_name or not args.registration_key or not identity_provided:
            print("❌ Error: Missing required arguments (name, registration-key, and email or token).")
            sys.exit(1)

        cmd = [
            sys.executable,
            str(AGENT_SCRIPT),
            "--device-name",
            args.device_name,
            "--registration-key",
            args.registration_key,
            "--device-type",
            args.device_type,
            "--api-url",
            args.api_url if args.api_url else PROD_API_URL,
        ]
        if args.token:
            cmd += ["--token", args.token]
        else:
            cmd += ["--email", args.email]
            if args.password:
                cmd += ["--password", args.password]
            # If no password, agent.py will prompt via getpass
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
