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
LLAMA_CPP_RELEASE = "b8808"

# Detect Virtual Environment Root and define Install Directory
if sys.prefix != sys.base_prefix:
    VENV_ROOT = Path(sys.prefix)
    BIN_LLAMA_DIR = VENV_ROOT / "bin-llama"
    BUILD_CACHE_DIR = VENV_ROOT / "build-cache"
else:
    potential_venv = PROJECT_ROOT / ".venv"
    if potential_venv.exists():
        BIN_LLAMA_DIR = potential_venv / "bin-llama"
        BUILD_CACHE_DIR = potential_venv / "build-cache"
    else:
        BIN_LLAMA_DIR = PROJECT_ROOT / "bin-llama"
        BUILD_CACHE_DIR = PROJECT_ROOT / "build-cache"


def _command_exists(name):
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
            cuda_tag = "cuda-13.1" if cuda[0] >= 13 else "cuda-12.4"
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
    """Download a prebuilt release archive and install binaries to bin_dir.

    Returns True on success, False on failure so the caller can fall back to cmake.
    Uses a tag file to skip re-download when already at the correct version.
    """
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
    """Clone llama.cpp at tag, build llama-server with cmake, and install to bin_dir.

    Uses a persistent build cache so subsequent runs with the same tag are skipped,
    and tag-change rebuilds are incremental where possible. ccache and Ninja are
    used automatically when available.
    """
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


def install_inference_engine():
    """Installs llama-server: prebuilt when available, build from source as fallback."""
    logger.info("Installing AI Inference Engine (llama.cpp)")

    BIN_LLAMA_DIR.mkdir(parents=True, exist_ok=True)
    tag = LLAMA_CPP_RELEASE

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


def main():
    """Main installation script."""
    logger.info("Starting Installation for Language Model...")
    install_inference_engine()
    logger.info("Language Model component installation complete.")


if __name__ == "__main__":
    main()
