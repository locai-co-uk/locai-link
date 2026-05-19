# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Pre-fetch native binaries for a Link PyInstaller bundle.

Each function stages one plugin's native binaries into
`_artifacts/<os>-<arch>/<bin-subdir>/` so PyInstaller can pick them up as
`datas`.  Plugin install.py modules are reused as-is — we just hand them a
custom destination directory and build-cache root.

Usage
-----
    uv run python -m bundling.prefetch                          # all plugins
    uv run python -m bundling.prefetch --plugin language_model  # subset
"""

import argparse
import importlib.util
import logging
import platform
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _load_install_module(plugin: str, build_cache_dir: Path) -> ModuleType:
    """Load a plugin's install.py in isolation, redirecting its BUILD_CACHE_DIR.

    The plugin isn't on sys.path at build time, and we want its archive cache
    to land under the build artefacts tree (not the developer's venv) so
    repeated builds cache cleanly per platform.
    """
    install_py = REPO_ROOT / "plugins" / plugin / "install.py"
    spec = importlib.util.spec_from_file_location(f"_link_install_for_{plugin}", install_py)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {install_py}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # setattr keeps pyright quiet about the dynamic plugin-local global.
    setattr(module, "BUILD_CACHE_DIR", build_cache_dir)
    return module


def _platform_tag(os_name: str, machine: str) -> str:
    arch = "arm64" if machine.lower() in ("arm64", "aarch64") else "x86_64"
    os_slug = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(os_name, os_name.lower())
    return f"{os_slug}-{arch}"


def prefetch_language_model(dest_root: Path) -> Path:
    """Stage llama-server + llama-swap into <dest_root>/bin-llama/."""
    bin_dir = dest_root / "bin-llama"
    bin_dir.mkdir(parents=True, exist_ok=True)

    install = _load_install_module("language_model", dest_root / "build-cache")
    cpp_tag = install.LLAMA_CPP_RELEASE
    swap_tag = install.LLAMA_SWAP_RELEASE

    # llama.cpp — required.  No prebuilt = no bundle (we don't compile at bundle time).
    cpp_url = install._prebuilt_url(cpp_tag)
    if cpp_url is None:
        raise SystemExit(
            f"No llama.cpp prebuilt for {platform.system()}/{platform.machine()}. "
            f"Bundling does not compile llama.cpp from source — pick a different runner or "
            f"install llama-server manually into {bin_dir} first."
        )
    if not install._install_prebuilt(cpp_url, bin_dir, cpp_tag):
        raise SystemExit(f"Failed to install llama.cpp prebuilt from {cpp_url}")

    # llama-swap — optional.
    swap_url = install._swap_prebuilt_url(swap_tag)
    if swap_url is None:
        logger.warning("No llama-swap prebuilt for this platform; multi-model serving disabled in bundle.")
    elif not install._install_swap_prebuilt(swap_url, bin_dir, swap_tag):
        raise SystemExit(f"Failed to install llama-swap prebuilt from {swap_url}")

    return bin_dir


def prefetch_audio_transcriber(dest_root: Path) -> Path:
    """Stage whisper-server into <dest_root>/bin-whisper/.

    whisper.cpp publishes Windows prebuilts only; Linux and macOS fall back to
    the plugin's cmake-build path (clones whisper.cpp at the pinned tag and
    builds whisper-server).  Build host needs `git` and `cmake`.
    """
    bin_dir = dest_root / "bin-whisper"
    bin_dir.mkdir(parents=True, exist_ok=True)

    install = _load_install_module("audio_transcriber", dest_root / "build-cache")
    tag = install.WHISPER_CPP_RELEASE

    url = install._prebuilt_url(tag)
    if url is not None:
        if install._install_prebuilt(url, bin_dir, tag):
            return bin_dir
        raise SystemExit(f"Failed to install whisper.cpp prebuilt from {url}")

    # No prebuilt for this platform — mirror install.py's source-build flags.
    cmake_flags = [
        "-DCMAKE_BUILD_TYPE=Release",
        "-DWHISPER_BUILD_SERVER=ON",
        "-DWHISPER_BUILD_EXAMPLES=ON",
        "-DBUILD_SHARED_LIBS=OFF",
    ]
    if platform.system() == "Darwin":
        cmake_flags.append("-DGGML_NATIVE=OFF")
    cmake_flags += install._detect_gpu_cmake_flags()
    install._cmake_build(tag, cmake_flags, bin_dir)
    return bin_dir


# Public dispatch table — keys match plugin directory names under plugins/.
PREFETCHERS = {
    "language_model": prefetch_language_model,
    "audio_transcriber": prefetch_audio_transcriber,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-fetch native binaries for a Link PyInstaller bundle.")
    parser.add_argument(
        "--plugin",
        action="append",
        choices=list(PREFETCHERS.keys()),
        help="Plugin to prefetch (repeatable). Default: every plugin with native deps.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Destination root (default: bundling/_artifacts/<os>-<arch>/).",
    )
    args = parser.parse_args()

    dest = args.out
    if dest is None:
        tag = _platform_tag(platform.system(), platform.machine())
        dest = Path(__file__).resolve().parent / "_artifacts" / tag

    plugins = args.plugin or list(PREFETCHERS.keys())
    for name in plugins:
        bin_dir = PREFETCHERS[name](dest)
        logger.info(f"[{name}] staged at {bin_dir}")


if __name__ == "__main__":
    main()
