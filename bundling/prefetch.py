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


# Executables that the bundle's plugins actually invoke at runtime. Anything
# else in the prefetched bin-* directories is dev/debug/bench tooling shipped
# alongside the real binaries in the upstream release archives — we delete it
# before PyInstaller picks it up so the bundle is smaller, signs faster, and
# notarises faster.
#
# Listed per-platform with extensions because the same set covers macOS / Linux
# (no extension) and Windows (.exe). Shared libraries (libllama.dylib,
# libggml-*.dylib, libmtmd.dylib, libwhisper.dylib, .so, .dll, .metallib, etc.)
# are NEVER pruned — they're identified by name prefix / suffix in
# _is_shared_library() and preserved unconditionally because llama-server /
# whisper-server dynamically link against them.
LLAMA_RUNTIME_EXECUTABLES: set[str] = {
    "llama-server",
    "llama-server.exe",
    "llama-swap",
    "llama-swap.exe",
}
WHISPER_RUNTIME_EXECUTABLES: set[str] = {
    "whisper-server",
    "whisper-server.exe",
}


def _is_shared_library(path: Path) -> bool:
    """True if ``path`` looks like a shared library or platform-data file
    that must be preserved regardless of the executable allowlist."""
    name = path.name.lower()
    if name.startswith("lib"):
        return True
    # Common shared-lib + GPU-shader extensions across the three OSes.
    suffixes = (".dylib", ".so", ".dll", ".metallib", ".metal")
    if name.endswith(suffixes):
        return True
    # Linux versioned shared libraries: libfoo.so.1, libfoo.so.1.2.3
    if ".so." in name:
        return True
    return False


# Mach-O / ELF / PE magic numbers. Used to confirm a candidate-for-deletion
# is actually an executable rather than a data file with a weird name.
_EXECUTABLE_MAGIC_PREFIXES = (
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",  # Mach-O 64-bit LE / BE
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",  # Mach-O 32-bit LE / BE
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",  # Universal "fat" binary
)


def _is_executable_file(path: Path) -> bool:
    """True if ``path``'s first bytes match a Mach-O, ELF, or PE executable."""
    try:
        with path.open("rb") as fh:
            head = fh.read(4)
    except OSError:
        return False
    if head in _EXECUTABLE_MAGIC_PREFIXES:
        return True
    if head.startswith(b"\x7fELF"):
        return True
    # All Windows PE files start with the MZ DOS stub.
    if head[:2] == b"MZ":
        return True
    return False


def _prune_unused_executables(bin_dir: Path, keep: set[str]) -> None:
    """Delete executables under ``bin_dir`` whose name isn't in ``keep``.

    Walks ``bin_dir`` recursively. For each regular file:

    - If it's a shared library (lib*, .dylib/.so/.dll/.metallib/.metal, or a
      versioned .so.N) it's preserved unconditionally.
    - If its name is in ``keep`` it's preserved.
    - Otherwise, only files that the magic-byte check identifies as Mach-O,
      ELF, or PE executables are deleted. Data files (READMEs, headers,
      configs) are left alone to keep the prune behaviour minimally invasive.

    A second pass cleans up symlinks whose target was deleted so notarisation
    doesn't choke on dangling links.

    Net effect on a typical llama.cpp release archive: ~25 dev/bench
    executables totalling 200-400 MB are removed, leaving just llama-server +
    llama-swap and all the shared libraries they link against.
    """
    dropped: list[tuple[str, int]] = []
    kept: list[str] = []

    for entry in sorted(bin_dir.rglob("*")):
        if not entry.is_file() or entry.is_symlink():
            continue
        if _is_shared_library(entry):
            kept.append(entry.name)
            continue
        if entry.name in keep:
            kept.append(entry.name)
            continue
        if not _is_executable_file(entry):
            # Plain data file with a non-shared-lib name — leave it; the
            # byte savings aren't worth the risk of deleting something
            # we don't recognise.
            kept.append(entry.name)
            continue
        try:
            size = entry.stat().st_size
            entry.unlink()
            dropped.append((entry.name, size))
        except OSError as exc:
            logger.warning(f"Could not delete {entry.name}: {exc}")

    # Drop dangling symlinks left behind by the deletion pass — some
    # llama.cpp builds include short-name symlinks pointing at versioned
    # binaries (rare for executables, common for shared libs). Skipping
    # this leaves notarytool with broken links and ddebug-level confusion.
    for entry in bin_dir.rglob("*"):
        if entry.is_symlink() and not entry.exists():
            try:
                entry.unlink()
            except OSError:
                pass

    if dropped:
        total_mb = sum(s for _, s in dropped) / 1024 / 1024
        logger.info(
            f"Pruned {len(dropped)} unused executables from {bin_dir.name} "
            f"({total_mb:.1f} MB freed): "
            f"{', '.join(sorted(name for name, _ in dropped))}"
        )
    else:
        logger.info(f"No unused executables to prune from {bin_dir.name}.")
    logger.info(f"Kept {len(kept)} files in {bin_dir.name}.")


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

    # Strip dev/bench tooling shipped alongside llama-server in the upstream
    # archive (llama-bench, llama-quantize, rpc-server, etc.). Each is its
    # own Mach-O that codesign+notarise spend real time on for zero runtime
    # value. Allowlist is the binaries the language_model plugin actually
    # invokes; everything else executable goes.
    _prune_unused_executables(bin_dir, LLAMA_RUNTIME_EXECUTABLES)

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
            # Defensive: whisper.cpp's Windows prebuilt is already filtered
            # to whisper-server.exe + DLLs at install time, but a future
            # release could change that. The prune is a no-op in steady
            # state and a safety net otherwise.
            _prune_unused_executables(bin_dir, WHISPER_RUNTIME_EXECUTABLES)
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
    # Source build only emits whisper-server as the cmake target, but the
    # examples build (-DWHISPER_BUILD_EXAMPLES=ON above) can drag in other
    # tools — prune them out of the bundle.
    _prune_unused_executables(bin_dir, WHISPER_RUNTIME_EXECUTABLES)
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
