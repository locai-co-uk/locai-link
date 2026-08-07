# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import http.client
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
PROJECT_ROOT = Path(__file__).parent

# Pinned whisper.cpp release — update manually after vetting a new release.
# Find release tags at: https://github.com/ggml-org/whisper.cpp/releases
WHISPER_CPP_RELEASE = "v1.9.2"

# Detect Virtual Environment Root and define Install Directory.
# FROZEN: running from a PyInstaller bundle.
FROZEN = bool(getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None))


def _resolve_install_dirs() -> tuple[Path, Path]:
    """Pick (bin-dir, build-cache-dir) based on frozen/venv/system layout."""
    if FROZEN:
        meipass = Path(getattr(sys, "_MEIPASS"))
        bin_dir = meipass / "bin-whisper"
        return bin_dir, bin_dir  # build cache never written in frozen mode; kept for parity
    if sys.prefix != sys.base_prefix:
        venv_root = Path(sys.prefix)
        return venv_root / "bin-whisper", venv_root / "build-cache"
    potential_venv = PROJECT_ROOT / ".venv"
    if potential_venv.exists():
        return potential_venv / "bin-whisper", potential_venv / "build-cache"
    return PROJECT_ROOT / "bin-whisper", PROJECT_ROOT / "build-cache"


BIN_WHISPER_DIR, BUILD_CACHE_DIR = _resolve_install_dirs()


def _command_exists(name: str) -> bool:
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
    """Returns the platform-appropriate prebuilt release URL, or None if unavailable.

    whisper.cpp only provides Windows prebuilts; Linux/macOS must build from source.
    """
    system = platform.system()
    cuda = _detect_cuda_version()
    base = f"https://github.com/ggml-org/whisper.cpp/releases/download/{tag}"

    if system == "Windows":
        if cuda:
            # CUDA 12.4 build is forward-compatible with CUDA 13.x via driver compatibility.
            cuda_zip = "11.8.0" if cuda[0] <= 11 else "12.4.0"
            logger.info(f"CUDA {cuda[0]}.{cuda[1]} detected — using cublas-{cuda_zip} build.")
            return f"{base}/whisper-cublas-{cuda_zip}-bin-x64.zip"
        # BLAS build: better CPU performance than plain bin via OpenBLAS.
        return f"{base}/whisper-blas-bin-x64.zip"

    # Linux and macOS: no prebuilts available — caller will fall back to cmake.
    return None


_DOWNLOAD_TIMEOUT = 30  # seconds per attempt; guards against a stalled CDN


def _download_with_retry(url: str, dest: Path, attempts: int = 3) -> None:
    """Download ``url`` to ``dest``, retrying transient network failures.

    GitHub's asset CDN can drop connections mid-transfer (esp. Windows). Each
    attempt is timeout-bounded; only transient errors retry (4xx fails fast).
    Streams to a ``.partial`` sidecar, renamed onto ``dest`` only on success.
    """
    partial = dest.with_suffix(dest.suffix + ".partial")
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as resp, open(partial, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            os.replace(partial, dest)
            return
        except urllib.error.HTTPError as e:
            partial.unlink(missing_ok=True)
            # 4xx is permanent, don't waste retries on it; 5xx is worth another go.
            if e.code < 500 or attempt == attempts:
                raise
            logger.warning(f"Download attempt {attempt}/{attempts} failed (HTTP {e.code}); retrying...")
            time.sleep(2 * attempt)
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as e:
            partial.unlink(missing_ok=True)
            if attempt == attempts:
                raise
            logger.warning(f"Download attempt {attempt}/{attempts} failed ({e}); retrying...")
            time.sleep(2 * attempt)


def _install_prebuilt(url, bin_dir, tag):
    """Download a prebuilt release archive and install binaries to bin_dir.

    Returns True on success, False on failure so the caller can fall back to cmake.
    Uses a tag file to skip re-download when already at the correct version.
    """
    system = platform.system()
    binary_filename = "whisper-server.exe" if system == "Windows" else "whisper-server"
    binary_dest = bin_dir / binary_filename

    # Tag-based caching. The tag file records provenance (prebuilt:<tag> /
    # source:<tag>); bare legacy tags count as prebuilt.
    safe_name = "whisper_cpp"
    cache_dir = BUILD_CACHE_DIR / safe_name
    tag_file = cache_dir / "tag"
    if binary_dest.exists() and tag_file.exists() and tag_file.read_text().strip() in (f"prebuilt:{tag}", tag):
        logger.info(f"whisper.cpp already installed ({tag}) — skipping download.")
        return True

    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / url.split("/")[-1]

    logger.info("Downloading whisper.cpp prebuilt binary...")
    try:
        _download_with_retry(url, archive_path)
    except Exception as e:
        logger.warning(f"Download failed: {e}")
        archive_path.unlink(missing_ok=True)
        return False

    logger.info("Extracting...")
    try:
        if archive_path.name.endswith(".tar.gz"):
            with tarfile.open(archive_path, "r:gz") as tf:
                members = tf.getmembers()
                binary_matches = [m.name for m in members if Path(m.name).name.lower() == binary_filename.lower()]
                if not binary_matches:
                    logger.warning(f"{binary_filename} not found in archive.")
                    return False

                for m in members:
                    if not m.isfile():
                        continue
                    f = tf.extractfile(m)
                    if f:
                        dest = bin_dir / Path(m.name).name
                        dest.write_bytes(f.read())
                        dest.chmod(0o755)

                # Create symlinks for versioned shared libraries
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

    tag_file.write_text(f"prebuilt:{tag}")
    logger.info(f"whisper.cpp installed to {bin_dir}")
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


def _missing_build_prerequisites():
    """Return (tool, install_hint) pairs for any missing build tools.

    whisper.cpp has no Linux/macOS prebuilt server binaries, so source builds
    need git and cmake on PATH. Reporting the full list at once is more useful
    than failing one at a time mid-build.
    """
    system = platform.system()
    if system == "Darwin":
        hint_prefix = "brew install"
    elif system == "Linux":
        # Don't assume a package manager — Debian/RHEL/Arch each use a different one.
        # Just give the canonical package names; the user can map to their distro.
        hint_prefix = "(install package)"
    elif system == "Windows":
        hint_prefix = "choco install"
    else:
        hint_prefix = "install"

    missing = []
    if not _command_exists("git"):
        missing.append(("git", f"{hint_prefix} git"))
    if not _command_exists("cmake"):
        missing.append(("cmake", f"{hint_prefix} cmake"))
    return missing


def _cmake_build(tag, cmake_flags, bin_dir):
    """Clone whisper.cpp at tag, build whisper-server with cmake, and install to bin_dir."""
    missing = _missing_build_prerequisites()
    if missing:
        tools = ", ".join(t for t, _ in missing)
        logger.error(
            f"Cannot build whisper.cpp from source: missing {tools}. "
            f"whisper.cpp does not publish prebuilt server binaries for "
            f"{platform.system()} at tag {tag}, so these tools are required."
        )
        for _, hint in missing:
            logger.error(f"  Install with: {hint}")
        if platform.system() == "Darwin":
            logger.error("  (If Homebrew is not installed: https://brew.sh — then run the command above.)")
        sys.exit(1)

    system = platform.system()
    binary_filename = "whisper-server.exe" if system == "Windows" else "whisper-server"
    cpu_count = os.cpu_count() or 2

    cache_dir = BUILD_CACHE_DIR / "whisper_cpp"
    src_dir = cache_dir / "src"
    build_dir = cache_dir / "build"
    tag_file = cache_dir / "tag"

    # Early-exit: already SOURCE-built at this exact tag. A cached prebuilt
    # never satisfies this path, so forcing a source build actually rebuilds.
    cached_tag = tag_file.read_text().strip() if tag_file.exists() else None
    binary_dest = bin_dir / binary_filename
    if binary_dest.exists() and cached_tag == f"source:{tag}":
        logger.info(f"whisper.cpp already built from source ({tag}) — skipping build.")
        return

    cache_dir.mkdir(parents=True, exist_ok=True)

    # The src tree only depends on the bare tag, whatever provenance the
    # installed binary has.
    bare_cached = cached_tag.split(":", 1)[-1] if cached_tag else None

    try:
        if bare_cached != tag and src_dir.exists():
            logger.info(f"Tag changed ({cached_tag} -> {tag}) — re-cloning...")
            shutil.rmtree(src_dir)
            if build_dir.exists():
                shutil.rmtree(build_dir)

        if not src_dir.exists():
            logger.info(f"Cloning whisper.cpp {tag}...")
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
                    "https://github.com/ggml-org/whisper.cpp.git",
                    str(src_dir),
                ],
                check=True,
            )

        configure_flags = list(cmake_flags)
        if _command_exists("ninja"):
            configure_flags = ["-G", "Ninja"] + configure_flags
        if _command_exists("ccache"):
            configure_flags += [
                "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
            ]

        logger.info("Configuring...")
        subprocess.run(
            ["cmake", "-S", str(src_dir), "-B", str(build_dir)] + configure_flags,
            check=True,
        )

        logger.info(f"Building whisper-server with {cpu_count} cores (this may take a few minutes)...")
        subprocess.run(
            ["cmake", "--build", str(build_dir), "--config", "Release", "--target", "whisper-server", f"-j{cpu_count}"],
            check=True,
        )

        found = next(build_dir.rglob(binary_filename), None)
        if not found:
            logger.error(f"{binary_filename} not found in build output.")
            sys.exit(1)

        shutil.copy2(found, binary_dest)
        if system != "Windows":
            binary_dest.chmod(0o755)

        tag_file.write_text(f"source:{tag}")
        logger.info(f"whisper.cpp built and installed to {bin_dir}")

    except subprocess.CalledProcessError as e:
        logger.error(f"Build failed (exit {e.returncode}).")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to install whisper.cpp: {e}")
        sys.exit(1)


def _is_already_installed(tag: str, force_source: bool = False) -> bool:
    """True when whisper-server binary is present and the cached tag matches.

    The tag file records provenance (prebuilt:<tag> / source:<tag>; bare
    legacy tags count as prebuilt). Under force_source only a source-built
    binary counts, so a cached prebuilt gets rebuilt instead of silently
    kept. In a frozen bundle the binary is shipped without a tag file, so
    presence alone counts as installed.
    """
    binary_filename = "whisper-server.exe" if platform.system() == "Windows" else "whisper-server"
    binary_dest = BIN_WHISPER_DIR / binary_filename
    if FROZEN:
        return binary_dest.exists()
    tag_file = BUILD_CACHE_DIR / "whisper_cpp" / "tag"
    if not (binary_dest.exists() and tag_file.exists()):
        return False
    recorded = tag_file.read_text().strip()
    accepted = {f"source:{tag}"} if force_source else {f"source:{tag}", f"prebuilt:{tag}", tag}
    return recorded in accepted


def install_inference_engine():
    """Installs whisper-server: prebuilt when available, build from source as fallback."""
    if FROZEN:
        return  # binary ships in the bundle; nothing to install
    BIN_WHISPER_DIR.mkdir(parents=True, exist_ok=True)
    tag = WHISPER_CPP_RELEASE

    # LOCAI_WHISPER_FORCE_SOURCE=1 skips the prebuilt: a native source build
    # sidesteps the prebuilt's CPU-variant loader, which can SIGILL on CPUs
    # without the newest instruction sets (observed on CI runners).
    force_source = os.environ.get("LOCAI_WHISPER_FORCE_SOURCE") == "1"
    if force_source and platform.system() == "Windows":
        logger.warning("LOCAI_WHISPER_FORCE_SOURCE ignored on Windows (prebuilt only).")
        force_source = False

    if _is_already_installed(tag, force_source=force_source):
        return  # silent no-op — caller decides whether to announce anything

    logger.info("Installing Audio Transcription Engine (whisper.cpp)")
    url = None if force_source else _prebuilt_url(tag)
    if url:
        if _install_prebuilt(url, BIN_WHISPER_DIR, tag):
            return
        if platform.system() == "Windows":
            logger.error("Failed to download whisper.cpp prebuilt.")
            logger.error(f"Download manually: https://github.com/ggml-org/whisper.cpp/releases/tag/{tag}")
            sys.exit(1)
        logger.info("Prebuilt download failed — falling back to building from source.")

    cmake_flags = [
        "-DCMAKE_BUILD_TYPE=Release",
        "-DWHISPER_BUILD_SERVER=ON",
        "-DWHISPER_BUILD_EXAMPLES=ON",
        "-DBUILD_SHARED_LIBS=OFF",
    ]

    # AppleClang rejects `-mcpu=native`, which ggml falls back to when ARM feature
    # detection fails. Disable ggml's native detection on macOS so it picks safe
    # baseline flags — Metal + Accelerate handle the perf-critical paths anyway.
    if platform.system() == "Darwin":
        cmake_flags.append("-DGGML_NATIVE=OFF")

    _cmake_build(
        tag=tag,
        cmake_flags=cmake_flags + _detect_gpu_cmake_flags(),
        bin_dir=BIN_WHISPER_DIR,
    )


def main():
    """Main installation script."""
    if _is_already_installed(WHISPER_CPP_RELEASE):
        return  # nothing to do, stay silent
    logger.info("Starting Installation for Audio Transcriber...")
    install_inference_engine()
    logger.info("Audio Transcriber component installation complete.")


if __name__ == "__main__":
    main()
