# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
PROJECT_ROOT = Path(__file__).parent

# Pinned llama.cpp release — update manually after vetting a new release.
# Find release tags at: https://github.com/ggml-org/llama.cpp/releases
LLAMA_CPP_RELEASE = "b9789"

# Pinned llama-swap release — update manually after vetting a new release.
# Find release tags at: https://github.com/mostlygeek/llama-swap/releases
# The asset URL pattern is `llama-swap_<tag>_<os>_<arch>.<ext>`.
LLAMA_SWAP_RELEASE = "216"

# Detect Virtual Environment Root and define Install Directory.
# FROZEN: running from a PyInstaller bundle.
FROZEN = bool(getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None))


def _resolve_install_dirs() -> tuple[Path, Path]:
    """Pick (bin-dir, build-cache-dir) based on frozen/venv/system layout."""
    if FROZEN:
        meipass = Path(getattr(sys, "_MEIPASS"))
        bin_dir = meipass / "bin-llama"
        return bin_dir, bin_dir  # build cache never written in frozen mode; kept for parity
    if sys.prefix != sys.base_prefix:
        venv_root = Path(sys.prefix)
        return venv_root / "bin-llama", venv_root / "build-cache"
    potential_venv = PROJECT_ROOT / ".venv"
    if potential_venv.exists():
        return potential_venv / "bin-llama", potential_venv / "build-cache"
    return PROJECT_ROOT / "bin-llama", PROJECT_ROOT / "build-cache"


BIN_LLAMA_DIR, BUILD_CACHE_DIR = _resolve_install_dirs()


def _command_exists(name: str):
    """Checks if a tool is available on the PATH."""
    return shutil.which(name) is not None


def _detect_cuda_version():
    """Detects the installed CUDA version. Returns (major, minor) or None."""
    if _command_exists("nvcc"):
        try:
            out = subprocess.run(["nvcc", "--version"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                if "release" in line.lower():
                    import re

                    m = re.search(r"release\s+(\d+)\.(\d+)", line)
                    if m:
                        return (int(m.group(1)), int(m.group(2)))
        except Exception:
            pass

    if _command_exists("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                if "CUDA Version:" in line:
                    token = line.split("CUDA Version:")[1].strip().split()[0]
                    major, minor = token.split(".")[:2]
                    return (int(major), int(minor))
        except Exception:
            pass

    return None


def _prebuilt_url(tag):
    """Returns the platform-appropriate prebuilt release URL, or None if unavailable."""
    system = platform.system()
    machine = platform.machine().lower()
    is_arm = machine in ("arm64", "aarch64")
    cuda = _detect_cuda_version()
    base = f"https://github.com/ggml-org/llama.cpp/releases/download/{tag}"

    if system == "Windows":
        if cuda:
            cuda_tag = "cuda-13.3" if cuda[0] >= 13 else "cuda-12.4"
            logger.info(f"CUDA {cuda[0]}.{cuda[1]} detected — using {cuda_tag} build.")
            return f"{base}/llama-{tag}-bin-win-{cuda_tag}-x64.zip"
        return f"{base}/llama-{tag}-bin-win-cpu-x64.zip"

    elif system == "Darwin":
        arch = "arm64" if is_arm else "x64"
        return f"{base}/llama-{tag}-bin-macos-{arch}.tar.gz"

    elif system == "Linux":
        if cuda and _command_exists("nvcc"):
            # Full CUDA toolkit present — cmake build with GGML_CUDA gives best performance.
            logger.info(f"CUDA {cuda[0]}.{cuda[1]} + nvcc detected — will build from source for CUDA acceleration.")
            return None
        # GPU present but no full toolkit → Vulkan prebuilt beats CPU-only.
        has_gpu = _command_exists("nvidia-smi") or _command_exists("rocm-smi")
        if has_gpu and not is_arm:
            logger.info("GPU detected — using Vulkan-accelerated prebuilt.")
            return f"{base}/llama-{tag}-bin-ubuntu-vulkan-x64.tar.gz"
        arch = "arm64" if is_arm else "x64"
        return f"{base}/llama-{tag}-bin-ubuntu-{arch}.tar.gz"

    return None


def _install_prebuilt(url, bin_dir, tag):
    """Download + extract a prebuilt llama.cpp archive. Returns True/False; tag-cached."""
    system = platform.system()
    binary_filename = "llama-server.exe" if system == "Windows" else "llama-server"
    binary_dest = bin_dir / binary_filename

    # Tag-based caching
    safe_name = "llama_cpp"
    cache_dir = BUILD_CACHE_DIR / safe_name
    tag_file = cache_dir / "tag"
    if binary_dest.exists() and tag_file.exists() and tag_file.read_text().strip() == tag:
        logger.info(f"llama.cpp already installed ({tag}) — skipping download.")
        return True

    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / url.split("/")[-1]

    logger.info("Downloading llama.cpp prebuilt binary...")
    try:
        urllib.request.urlretrieve(url, archive_path)
    except Exception as e:
        logger.warning(f"Download failed: {e}")
        archive_path.unlink(missing_ok=True)
        return False

    logger.info("Extracting...")
    try:
        # Wipe stale llama.cpp artefacts from a prior tag so versioned shared
        # libraries (e.g. libllama.so.0.0.9222 alongside leftover .0.0.8808)
        # don't accumulate. Leaves llama-swap and any non-matching files alone.
        patterns = ["lib*"]
        patterns += ["llama-server.exe", "*.dll"] if system == "Windows" else ["llama-server"]
        for pattern in patterns:
            for stale in bin_dir.glob(pattern):
                if stale.is_file() or stale.is_symlink():
                    stale.unlink(missing_ok=True)

        is_targz = archive_path.name.endswith(".tar.gz")
        if is_targz:
            with tarfile.open(archive_path, "r:gz") as tf:
                members = tf.getmembers()
                names = [m.name for m in members]
                binary_matches = [n for n in names if Path(n).name.lower() == binary_filename.lower()]
                if not binary_matches:
                    logger.warning(f"{binary_filename} not found in archive.")
                    return False

                # Extract all regular files — the binary is dynamically linked and
                # the tarball includes the .so/.dylib files it depends on.
                for m in members:
                    if not m.isfile():
                        continue
                    f = tf.extractfile(m)
                    if f:
                        dest = bin_dir / Path(m.name).name
                        dest.write_bytes(f.read())
                        dest.chmod(0o755)

                # Second pass: create symlinks (versioned .so/.dylib names like
                # libmtmd.0.dylib -> libmtmd.dylib must exist or the binary crashes).
                for m in members:
                    if not m.issym():
                        continue
                    link_path = bin_dir / Path(m.name).name
                    target_name = Path(m.linkname).name
                    if not link_path.exists():
                        try:
                            link_path.symlink_to(target_name)
                        except OSError:
                            pass

            # macOS: strip quarantine attribute so Gatekeeper doesn't block execution
            if system == "Darwin":
                subprocess.run(
                    ["xattr", "-dr", "com.apple.quarantine", str(bin_dir)],
                    capture_output=True,
                )
        else:
            with zipfile.ZipFile(archive_path, "r") as zf:
                names = zf.namelist()
                binary_matches = [n for n in names if Path(n).name.lower() == binary_filename.lower()]
                if not binary_matches:
                    logger.warning(f"{binary_filename} not found in archive.")
                    return False
                to_extract = set(binary_matches)
                # On Windows, pull all DLLs too (ggml.dll, llama.dll, etc.)
                if system == "Windows":
                    to_extract |= {n for n in names if n.lower().endswith(".dll")}
                for member in to_extract:
                    (bin_dir / Path(member).name).write_bytes(zf.read(member))
                if system != "Windows":
                    binary_dest.chmod(0o755)
    except Exception as e:
        logger.warning(f"Extraction failed: {e}")
        return False
    finally:
        archive_path.unlink(missing_ok=True)

    tag_file.write_text(tag)
    logger.info(f"llama.cpp installed to {bin_dir}")
    return True


def _detect_gpu_cmake_flags():
    """Returns cmake GPU-acceleration flags for the current machine."""
    flags = []
    if platform.system() == "Linux" and _command_exists("nvidia-smi"):
        try:
            res = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                if _command_exists("nvcc"):
                    logger.info("NVIDIA GPU + CUDA Toolkit detected — enabling CUDA.")
                    flags.append("-DGGML_CUDA=ON")
                else:
                    logger.info("NVIDIA GPU detected but CUDA Toolkit (nvcc) not found — building CPU-only.")
        except Exception:
            pass
    # macOS Metal is auto-detected by cmake on Apple Silicon — no flag needed
    return flags


def _cmake_build(tag, cmake_flags, bin_dir):
    """Clone llama.cpp@tag, cmake-build llama-server, install to bin_dir. ccache/Ninja used if present."""
    if not _command_exists("git"):
        logger.error("git is required to build from source but was not found.")
        sys.exit(1)
    if not _command_exists("cmake"):
        logger.error("cmake is required to build from source but was not found.")
        sys.exit(1)

    system = platform.system()
    binary_filename = "llama-server.exe" if system == "Windows" else "llama-server"
    cpu_count = os.cpu_count() or 2

    cache_dir = BUILD_CACHE_DIR / "llama_cpp"
    src_dir = cache_dir / "src"
    build_dir = cache_dir / "build"
    tag_file = cache_dir / "tag"

    # Early-exit: already installed at this exact tag
    cached_tag = tag_file.read_text().strip() if tag_file.exists() else None
    binary_dest = bin_dir / binary_filename
    if binary_dest.exists() and cached_tag == tag:
        logger.info(f"llama.cpp already installed ({tag}) — skipping build.")
        return

    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Source checkout
        if cached_tag != tag and src_dir.exists():
            logger.info(f"Tag changed ({cached_tag} -> {tag}) — re-cloning...")
            shutil.rmtree(src_dir)
            if build_dir.exists():
                shutil.rmtree(build_dir)

        if not src_dir.exists():
            logger.info(f"Cloning llama.cpp {tag}...")
            subprocess.run(
                [
                    "git",
                    "-c",
                    "advice.detachedHead=false",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    tag,
                    "https://github.com/ggml-org/llama.cpp.git",
                    str(src_dir),
                ],
                check=True,
            )

        # Build acceleration
        configure_flags = list(cmake_flags)
        if _command_exists("ninja"):
            configure_flags = ["-G", "Ninja"] + configure_flags
        if _command_exists("ccache"):
            configure_flags += [
                "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
            ]

        # Configure
        logger.info("Configuring...")
        subprocess.run(
            ["cmake", "-S", str(src_dir), "-B", str(build_dir)] + configure_flags,
            check=True,
        )

        # Build
        logger.info(f"Building llama-server with {cpu_count} cores (this may take a few minutes)...")
        subprocess.run(
            ["cmake", "--build", str(build_dir), "--config", "Release", "--target", "llama-server", f"-j{cpu_count}"],
            check=True,
        )

        # Install — binary location varies by platform/generator
        found = next(build_dir.rglob(binary_filename), None)
        if not found:
            logger.error(f"{binary_filename} not found in build output.")
            sys.exit(1)

        shutil.copy2(found, binary_dest)
        if system != "Windows":
            binary_dest.chmod(0o755)

        tag_file.write_text(tag)
        logger.info(f"llama.cpp built and installed to {bin_dir}")

    except subprocess.CalledProcessError as e:
        logger.error(f"Build failed (exit {e.returncode}).")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to install llama.cpp: {e}")
        sys.exit(1)


def _is_already_installed(tag: str) -> bool:
    """True if llama-server is on disk and at ``tag``. Frozen bundles skip the tag check."""
    binary_filename = "llama-server.exe" if platform.system() == "Windows" else "llama-server"
    binary_dest = BIN_LLAMA_DIR / binary_filename
    if FROZEN:
        return binary_dest.exists()
    tag_file = BUILD_CACHE_DIR / "llama_cpp" / "tag"
    return binary_dest.exists() and tag_file.exists() and tag_file.read_text().strip() == tag


def install_inference_engine():
    """Installs llama-server: prebuilt when available, build from source as fallback."""
    if FROZEN:
        return  # binary ships in the bundle; nothing to install
    BIN_LLAMA_DIR.mkdir(parents=True, exist_ok=True)
    tag = LLAMA_CPP_RELEASE

    if _is_already_installed(tag):
        return  # silent no-op — caller decides whether to announce anything

    logger.info("Installing AI Inference Engine (llama.cpp)")
    url = _prebuilt_url(tag)
    if url:
        if _install_prebuilt(url, BIN_LLAMA_DIR, tag):
            return
        if platform.system() == "Windows":
            logger.error("Failed to download llama.cpp prebuilt.")
            logger.error(f"Download manually: https://github.com/ggml-org/llama.cpp/releases/tag/{tag}")
            sys.exit(1)
        logger.info("Prebuilt download failed — falling back to building from source.")

    cmake_flags = [
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_SHARED_LIBS=OFF",
        "-DLLAMA_BUILD_TESTS=OFF",
    ]

    # AppleClang rejects `-mcpu=native`, which ggml falls back to when ARM feature
    # detection fails. Disable ggml's native detection on macOS so it picks safe
    # baseline flags — Metal + Accelerate handle the perf-critical paths anyway.
    if platform.system() == "Darwin":
        cmake_flags.append("-DGGML_NATIVE=OFF")

    _cmake_build(
        tag=tag,
        cmake_flags=cmake_flags + _detect_gpu_cmake_flags(),
        bin_dir=BIN_LLAMA_DIR,
    )


def _swap_prebuilt_url(tag: str) -> str | None:
    """Platform-appropriate llama-swap release URL, or None. Asset names: llama-swap_<tag>_<os>_<arch>.{tar.gz,zip}."""
    system = platform.system()
    machine = platform.machine().lower()
    is_arm = machine in ("arm64", "aarch64")
    base = f"https://github.com/mostlygeek/llama-swap/releases/download/v{tag}"

    if system == "Linux":
        arch = "arm64" if is_arm else "amd64"
        return f"{base}/llama-swap_{tag}_linux_{arch}.tar.gz"
    if system == "Darwin":
        arch = "arm64" if is_arm else "amd64"
        return f"{base}/llama-swap_{tag}_darwin_{arch}.tar.gz"
    if system == "Windows":
        # Only amd64 is published. arm64 Windows users would need to wait for
        # upstream support (or build from source).
        if is_arm:
            return None
        return f"{base}/llama-swap_{tag}_windows_amd64.zip"
    return None


def _is_swap_installed(tag: str) -> bool:
    """True if llama-swap is on disk and at ``tag``. Frozen bundles skip the tag check."""
    binary_filename = "llama-swap.exe" if platform.system() == "Windows" else "llama-swap"
    binary_dest = BIN_LLAMA_DIR / binary_filename
    if FROZEN:
        return binary_dest.exists()
    tag_file = BUILD_CACHE_DIR / "llama_swap" / "tag"
    return binary_dest.exists() and tag_file.exists() and tag_file.read_text().strip() == tag


def _install_swap_prebuilt(url: str, bin_dir: Path, tag: str) -> bool:
    """Download + extract a llama-swap release. Returns True/False; tag-cached."""
    system = platform.system()
    binary_filename = "llama-swap.exe" if system == "Windows" else "llama-swap"
    binary_dest = bin_dir / binary_filename

    cache_dir = BUILD_CACHE_DIR / "llama_swap"
    tag_file = cache_dir / "tag"
    if binary_dest.exists() and tag_file.exists() and tag_file.read_text().strip() == tag:
        logger.info(f"llama-swap already installed ({tag}) — skipping download.")
        return True

    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / url.split("/")[-1]

    logger.info(f"Downloading llama-swap {tag}...")
    try:
        urllib.request.urlretrieve(url, archive_path)
    except Exception as e:
        logger.warning(f"Download failed: {e}")
        archive_path.unlink(missing_ok=True)
        return False

    logger.info("Extracting llama-swap...")
    try:
        if archive_path.name.endswith(".tar.gz"):
            with tarfile.open(archive_path, "r:gz") as tf:
                matches = [
                    m for m in tf.getmembers() if m.isfile() and Path(m.name).name.lower() == binary_filename.lower()
                ]
                if not matches:
                    logger.warning(f"{binary_filename} not found in archive.")
                    return False
                f = tf.extractfile(matches[0])
                if f is None:
                    logger.warning(f"Could not extract {binary_filename}.")
                    return False
                binary_dest.write_bytes(f.read())
                binary_dest.chmod(0o755)

            # macOS Gatekeeper quarantine attribute strip — same as llama-server.
            if system == "Darwin":
                subprocess.run(
                    ["xattr", "-dr", "com.apple.quarantine", str(binary_dest)],
                    capture_output=True,
                )
        else:
            with zipfile.ZipFile(archive_path, "r") as zf:
                matches = [n for n in zf.namelist() if Path(n).name.lower() == binary_filename.lower()]
                if not matches:
                    logger.warning(f"{binary_filename} not found in archive.")
                    return False
                binary_dest.write_bytes(zf.read(matches[0]))
                # No chmod needed on Windows.
    except Exception as e:
        logger.warning(f"Extraction failed: {e}")
        return False
    finally:
        archive_path.unlink(missing_ok=True)

    tag_file.write_text(tag)
    logger.info(f"llama-swap installed to {binary_dest}")
    return True


def install_swap():
    """Install the pinned llama-swap binary. Soft-fails on unsupported platforms (single-serve still works)."""
    if FROZEN:
        return  # binary ships in the bundle; nothing to install
    BIN_LLAMA_DIR.mkdir(parents=True, exist_ok=True)
    tag = LLAMA_SWAP_RELEASE

    if _is_swap_installed(tag):
        return

    url = _swap_prebuilt_url(tag)
    if not url:
        logger.warning(
            f"No llama-swap prebuilt available for {platform.system()}/{platform.machine()}; "
            "multi-model swap serving will be disabled on this device."
        )
        return

    if not _install_swap_prebuilt(url, BIN_LLAMA_DIR, tag):
        logger.warning("llama-swap install failed; multi-model swap serving disabled.")


def main():
    """Main installation script."""
    if _is_already_installed(LLAMA_CPP_RELEASE) and _is_swap_installed(LLAMA_SWAP_RELEASE):
        return  # nothing to do, stay silent
    logger.info("Starting Installation for Language Model...")
    install_inference_engine()
    install_swap()
    logger.info("Language Model component installation complete.")


if __name__ == "__main__":
    main()
