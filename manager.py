# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import argparse
import os
import platform
import shutil
import subprocess
import sys
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

# Pinned llama.cpp release — update manually after vetting a new release.
# Find release tags at: https://github.com/ggml-org/llama.cpp/releases
LLAMA_CPP_RELEASE = "b8705"

# Pinned whisper.cpp release — update manually after vetting a new release.
# Find release tags at: https://github.com/ggml-org/whisper.cpp/releases
WHISPER_CPP_RELEASE = "v1.8.4"

# Persistent cmake build cache — survives between runs for incremental builds.
# Lives inside .venv so it is cleaned up by `manager.py reset`.
BUILD_CACHE_DIR = VENV_PATH / "build-cache"

# Exit code the agent uses to signal "update and restart me"
EXIT_CODE_UPDATE = 42

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
        print(f"ERROR: Failed to install uv: {e}")
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
            print(f"ERROR: Failed to switch to venv: {e}")
            sys.exit(1)
    else:
        print("ERROR: Virtual environment not found.")
        print("Please run: python manager.py setup")
        sys.exit(1)


def install_git(required_for=None):
    """Prompts to install git via the system package manager."""
    context = f" to build {required_for}" if required_for else ""
    print(f"\ngit is missing and required{context}.")

    system = platform.system()

    if system == "Darwin":
        if command_exists("brew"):
            install_cmd = ["brew", "install", "git"]
            install_desc = "brew install git"
        else:
            print("   Install via Xcode Command Line Tools:  xcode-select --install")
            print("   Or install Homebrew first: https://brew.sh")
            sys.exit(1)

    elif system == "Linux":
        if command_exists("apt-get"):
            install_cmd = ["sudo", "apt-get", "install", "-y", "git"]
            install_desc = "sudo apt-get install -y git"
        elif command_exists("dnf"):
            install_cmd = ["sudo", "dnf", "install", "-y", "git"]
            install_desc = "sudo dnf install -y git"
        elif command_exists("pacman"):
            install_cmd = ["sudo", "pacman", "-S", "--noconfirm", "git"]
            install_desc = "sudo pacman -S --noconfirm git"
        else:
            print("ERROR: No supported package manager found (apt-get, dnf, pacman).")
            print("   Install git manually: https://git-scm.com/downloads")
            sys.exit(1)

    elif system == "Windows":
        if command_exists("winget"):
            install_cmd = ["winget", "install", "--id", "Git.Git", "-e", "--source", "winget"]
            install_desc = "winget install --id Git.Git"
        elif command_exists("choco"):
            install_cmd = ["choco", "install", "git", "-y"]
            install_desc = "choco install git"
        else:
            print("ERROR: No package manager found (winget, choco).")
            print("   Install git from: https://git-scm.com/downloads/win")
            sys.exit(1)

    else:
        print(f"ERROR: Cannot auto-install git on {system}.")
        print("   Install git manually: https://git-scm.com/downloads")
        sys.exit(1)

    print(f"This will run: {install_desc}")
    try:
        confirm = input("Install git now? [Y/n] ").strip().lower()
    except EOFError:
        confirm = ""  # non-interactive — treat as yes

    if confirm not in ("", "y", "yes"):
        print("git installation skipped. Install it manually and re-run.")
        sys.exit(0)

    try:
        subprocess.run(install_cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: git installation failed: {e}")
        sys.exit(1)

    if not command_exists("git"):
        print("ERROR: git still not found after installation. You may need to open a new terminal.")
        sys.exit(1)

    print("OK: git installed successfully.")


def install_cmake(required_for=None):
    """Installs cmake into the project venv via uv pip."""
    context = f" to build {required_for}" if required_for else ""
    print(f"\ncmake is missing and required{context}.")
    print("It can be installed into the project environment (.venv) via uv — no sudo required.")

    try:
        confirm = input("Install cmake now? [Y/n] ").strip().lower()
    except EOFError:
        confirm = ""  # non-interactive — treat as yes

    if confirm not in ("", "y", "yes"):
        print("cmake installation skipped. Install it manually and re-run.")
        sys.exit(0)

    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(VENV_PATH)

    try:
        subprocess.run(["uv", "pip", "install", "cmake"], env=env, cwd=PROJECT_ROOT, check=True)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: cmake installation failed: {e}")
        sys.exit(1)

    # Add venv bin to PATH so cmake is usable immediately in this process
    venv_bin = VENV_PATH / ("Scripts" if platform.system() == "Windows" else "bin")
    os.environ["PATH"] = str(venv_bin) + os.pathsep + os.environ.get("PATH", "")

    if not command_exists("cmake"):
        print("ERROR: cmake still not found after installation. Re-run this command.")
        sys.exit(1)

    print("OK: cmake installed into .venv successfully.")


def _detect_gpu_cmake_flags():
    """Returns cmake GPU-acceleration flags for the current machine."""
    flags = []
    if platform.system() == "Linux" and command_exists("nvidia-smi"):
        try:
            res = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                if command_exists("nvcc"):
                    print("NVIDIA GPU + CUDA Toolkit detected — enabling CUDA.")
                    flags.append("-DGGML_CUDA=ON")
                else:
                    print("NVIDIA GPU detected but CUDA Toolkit (nvcc) not found — building CPU-only.")
                    print(
                        "   To enable CUDA: install the CUDA Toolkit from https://developer.nvidia.com/cuda-downloads"
                    )
        except Exception:
            pass
    # macOS Metal is auto-detected by cmake on Apple Silicon — no flag needed
    return flags


def _copy_mingw_runtime_dlls(dest_dir: Path):
    """Copy MinGW runtime DLLs alongside a freshly built Windows binary.

    Windows DLL search checks the binary's own directory before PATH entries,
    so placing the exact runtime versions used at compile time there prevents
    STATUS_ENTRYPOINT_NOT_FOUND (0xC0000139) caused by an older system-level
    DLL shadowing the MinGW one.
    """
    runtime_dlls = [
        "libstdc++-6.dll",
        "libgcc_s_seh-1.dll",
        "libwinpthread-1.dll",
    ]
    # Prefer the directory of whichever gcc is on PATH (matches the compiler used),
    # then fall back to common MinGW installation directories.
    search_dirs = []
    gcc = shutil.which("gcc")
    if gcc:
        search_dirs.append(Path(gcc).parent)
    search_dirs += [
        Path("C:/mingw64/bin"),
        Path("C:/msys64/mingw64/bin"),
        Path("C:/msys64/ucrt64/bin"),
    ]

    for dll_name in runtime_dlls:
        for src_dir in search_dirs:
            src = src_dir / dll_name
            if src.exists():
                dst = dest_dir / dll_name
                if not dst.exists():
                    shutil.copy2(src, dst)
                    print(f"  Copied {dll_name} from {src_dir}")
                break


def _cmake_build(display_name, repo_url, tag, cmake_flags, binary_name, bin_dir):
    """Clone repo at tag, build one binary target with cmake, and install it to bin_dir.

    Uses a persistent build cache under .venv/build-cache/ so subsequent runs with
    the same tag are skipped entirely, and tag-change rebuilds are incremental where
    possible.  ccache and Ninja are used automatically when available.
    """
    if not command_exists("git"):
        install_git(required_for=display_name)

    if not command_exists("cmake"):
        install_cmake(required_for=display_name)

    system = platform.system()
    binary_filename = f"{binary_name}.exe" if system == "Windows" else binary_name
    cpu_count = os.cpu_count() or 2

    # Persistent per-project cache directory
    safe_name = display_name.replace(" ", "_").replace(".", "_")
    cache_dir = BUILD_CACHE_DIR / safe_name
    src_dir = cache_dir / "src"
    build_dir = cache_dir / "build"
    tag_file = cache_dir / "tag"

    # --- Early-exit: already installed at this exact tag ---
    cached_tag = tag_file.read_text().strip() if tag_file.exists() else None
    binary_dest = bin_dir / binary_filename
    if binary_dest.exists() and cached_tag == tag:
        if system == "Windows":
            _copy_mingw_runtime_dlls(bin_dir)  # idempotent — skips DLLs already present
        print(f"OK: {display_name} already installed ({tag}) — skipping build.")
        return

    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        # --- Source checkout ---
        if cached_tag != tag and src_dir.exists():
            print(f"Tag changed ({cached_tag} → {tag}) — re-cloning {display_name}...")
            shutil.rmtree(src_dir)
            if build_dir.exists():
                shutil.rmtree(build_dir)

        if not src_dir.exists():
            print(f"Cloning {display_name} {tag}...")
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", tag, repo_url, str(src_dir)],
                check=True,
            )

        # --- Build acceleration ---
        configure_flags = list(cmake_flags)

        # Ninja: faster dependency resolution than Make
        if command_exists("ninja"):
            configure_flags = ["-G", "Ninja"] + configure_flags

        # ccache: object-level compiler cache — big win on repeated/incremental builds
        if command_exists("ccache"):
            configure_flags += [
                "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
            ]

        # --- Configure ---
        print("Configuring...")
        subprocess.run(
            ["cmake", "-S", str(src_dir), "-B", str(build_dir)] + configure_flags,
            check=True,
        )

        # --- Build ---
        print(f"Building {binary_name} with {cpu_count} cores (this may take a few minutes)...")
        subprocess.run(
            [
                "cmake",
                "--build",
                str(build_dir),
                "--config",
                "Release",
                "--target",
                binary_name,
                f"-j{cpu_count}",
            ],
            check=True,
        )

        # --- Install ---
        # Binary location varies by platform/generator — search recursively
        found = next(build_dir.rglob(binary_filename), None)
        if not found:
            print(f"ERROR: {binary_filename} not found in build output.")
            sys.exit(1)

        shutil.copy2(found, binary_dest)
        if system != "Windows":
            binary_dest.chmod(0o755)
        else:
            _copy_mingw_runtime_dlls(bin_dir)

        # Record the installed tag so future runs can skip the build
        tag_file.write_text(tag)

        print(f"OK: {display_name} installed to {bin_dir}")

    except subprocess.CalledProcessError as e:
        print(f"ERROR: Build failed (exit {e.returncode}).")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Failed to install {display_name}: {e}")
        sys.exit(1)


def install_llama_server():
    """Builds and installs llama-server from llama.cpp source."""
    print_step("Building LLM Inference Engine (llama.cpp)")
    bin_dir = VENV_PATH / "bin-llama"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _cmake_build(
        display_name="llama.cpp",
        repo_url="https://github.com/ggml-org/llama.cpp.git",
        tag=LLAMA_CPP_RELEASE,
        cmake_flags=[
            "-DCMAKE_BUILD_TYPE=Release",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DLLAMA_BUILD_TESTS=OFF",
        ]
        + _detect_gpu_cmake_flags(),
        binary_name="llama-server",
        bin_dir=bin_dir,
    )


def install_whisper_server():
    """Builds and installs whisper-server from whisper.cpp source."""
    print_step("Building Whisper Transcription Engine (whisper.cpp)")
    bin_dir = VENV_PATH / "bin-whisper"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _cmake_build(
        display_name="whisper.cpp",
        repo_url="https://github.com/ggml-org/whisper.cpp.git",
        tag=WHISPER_CPP_RELEASE,
        cmake_flags=[
            "-DCMAKE_BUILD_TYPE=Release",
            "-DWHISPER_BUILD_SERVER=ON",
            "-DWHISPER_BUILD_EXAMPLES=ON",
            "-DBUILD_SHARED_LIBS=OFF",
        ]
        + _detect_gpu_cmake_flags(),
        binary_name="whisper-server",
        bin_dir=bin_dir,
    )


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

    install_llama_server()
    install_whisper_server()

    print_step("Installing Project Dependencies")
    install_target = f"-e .[{extras}]" if extras else "-e ."

    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(VENV_PATH)
    subprocess.run(["uv", "pip", "install"] + install_target.split(), cwd=PROJECT_ROOT, env=env, check=True)

    print("\nSetup Complete.")


def get_local_version():
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


def get_current_branch(repo_dir: Path):
    """Returns the current git branch name, or None if it cannot be determined."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch and branch != "HEAD" else None


def update(repo_dir: Path, branch: str = DEFAULT_BRANCH) -> bool:
    """Pulls the latest code from the remote, stashing any local changes.

    Returns True if the codebase was updated, False if already up to date.
    """
    print_step("Checking for Updates")

    if not command_exists("git"):
        install_git(required_for="updates")

    # Use the actual current branch rather than the default, so running
    # install/update on a dev branch doesn't pull main into it.
    current_branch = get_current_branch(repo_dir)
    if current_branch and current_branch != branch:
        print(f"Detected branch '{current_branch}' — updating from origin/{current_branch}.")
        branch = current_branch

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
            print("ERROR: Could not stash local changes. Aborting update to avoid data loss.")
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
            print("WARNING:  Update succeeded but stash could not be re-applied cleanly.")
            print("   Your changes are saved in git stash — run 'git stash show' to review.")
        else:
            print("Local changes re-applied successfully.")

    # Re-install dependencies in case pyproject.toml changed
    print("Updating dependencies...")
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(VENV_PATH)
    subprocess.run(["uv", "pip", "install", "-e", "."], cwd=repo_dir, env=env, check=True)

    new_ver = get_local_version()
    print(f"OK: Update complete{f' — now at v{new_ver}' if new_ver else ''}.")
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
            print("ERROR: Error: API URL is required when using --dev.")
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
        install_git()

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
        print("ERROR: Error: manager.py not found in target directory.")
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
        print("ERROR: Error: Device name, registration key, and an identity (--email or --token) are required.")
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
        print(f"\nERROR: Installation step failed (Exit Code: {e.returncode})")
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
    subparsers.add_parser("install-deps", help="Download/build llama-server and whisper-server binaries")

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
        install_llama_server()
        install_whisper_server()
        return

    elif args.command == "update":
        update(PROJECT_ROOT, args.branch)
        return

    # Commands that REQUIRE the Virtual Env
    ensure_venv_execution()

    if args.command == "register":
        identity_provided = args.token or args.email
        if not args.device_name or not args.registration_key or not identity_provided:
            print("ERROR: Error: Missing required arguments (name, registration-key, and email or token).")
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
            print("ERROR: Error: activate requires either --api-key or --registration-key")
            sys.exit(1)

        if args.device_type:
            cmd.extend(["--device-type", args.device_type])

        cmd.extend(["--api-url", args.api_url if args.api_url else PROD_API_URL])
        subprocess.run(cmd, check=True)

    elif args.command == "run":
        if not CONFIG_FILE.exists():
            print("ERROR: Error: Device not configured. Run 'register' first.")
            sys.exit(1)

        cmd = [sys.executable, str(AGENT_SCRIPT)]
        if args.api_url:
            cmd.extend(["--api-url", args.api_url])

        while True:
            result = subprocess.run(cmd)
            if result.returncode == EXIT_CODE_UPDATE:
                print_step("OTA Update Requested")
                update(PROJECT_ROOT, DEFAULT_BRANCH)
                install_llama_server()
                install_whisper_server()
                print("Restarting agent...")
            else:
                # Normal exit or crash — don't restart
                sys.exit(result.returncode)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(0)
    except subprocess.CalledProcessError as e:
        print(f"\nERROR: Command execution failed (Exit Code: {e.returncode})")

        cmd_str = str(e.cmd)
        if "agent.py" in cmd_str:
            print("   The agent process crashed. Please check the logs above for details.")
        elif "git" in cmd_str:
            print("   Git operation failed. Check your internet connection or permissions.")
        elif "uv" in cmd_str:
            print("   Dependency installation failed.")

        sys.exit(e.returncode)
    except Exception as e:
        print(f"\nERROR: An unexpected error occurred: {e}")
        raise e
        sys.exit(1)
