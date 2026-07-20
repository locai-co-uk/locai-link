# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end Link bundle build.

A bundle is identified by the plugin set compiled into it. The artifact
name is derived from that plugin set (see ``bundling/manifest.py`` for
the codes and ordering). There is no separate "profile" concept — the
plugin list IS the recipe.

Bare (zero-plugin) bundles aren't a release shape; for that, use the
source install path (``curl … | bash`` from the README).

Examples::

    uv run python bundling/build.py --plugins language_model
    uv run python bundling/build.py --plugins language_model audio_transcriber

Steps:
    1. Validate plugin selection against the known + codable set.
    2. Pre-fetch native binaries needed by the selected plugins.
    3. Editable-install each plugin so its dist-info is visible to PyInstaller.
    4. Run PyInstaller (with LOCAI_BUNDLE_PLUGINS in the env).
    5. Write manifest.json into the bundle root.
"""

import argparse
import logging
import os
import platform as _pf
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from manifest import (
    PLUGIN_CODES,
    derive_asset_name,
    write_manifest,
)
from prefetch import PREFETCHERS, _platform_tag

SPEC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SPEC_DIR.parent
SPEC_FILE = SPEC_DIR / "locai-link.spec"
CRATES_DIR = REPO_ROOT / "crates"
LAUNCHER_DIR = CRATES_DIR / "launcher"
# Cargo workspace target — `cargo build` from any member crate writes here.
CARGO_TARGET_DIR = CRATES_DIR / "target"
LAUNCHER_BINARY_NAME = "locai-link.exe" if sys.platform == "win32" else "locai-link"

# Bundleable plugins — the keys of PLUGIN_CODES, in their canonical order.
# Anything outside this set is a config error at parse time; bundling a
# plugin without a code would produce an un-nameable artifact.
KNOWN_PLUGINS: list[str] = list(PLUGIN_CODES.keys())

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _resolve_plugins(args: argparse.Namespace) -> tuple[str, ...]:
    if not args.plugins:
        raise SystemExit(
            "No plugins selected. Pass --plugins <name> [<name> ...].\n"
            f"Bundleable plugins: {', '.join(KNOWN_PLUGINS)}\n"
            "Bare bundles aren't a release shape; use the curl source install instead."
        )
    unknown = [p for p in args.plugins if p not in KNOWN_PLUGINS]
    if unknown:
        raise SystemExit(f"Unknown plugins: {', '.join(unknown)}\nBundleable plugins: {', '.join(KNOWN_PLUGINS)}")
    # De-dupe + canonicalise via PLUGIN_CODES order
    seen = set(args.plugins)
    return tuple(p for p in KNOWN_PLUGINS if p in seen)


def run_prefetch(plugins: tuple[str, ...], tag: str) -> None:
    artifacts_root = SPEC_DIR / "_artifacts" / tag
    for name in plugins:
        prefetcher = PREFETCHERS.get(name)
        if prefetcher is None:
            continue
        logger.info(f"Pre-fetching native binaries for {name}")
        prefetcher(artifacts_root)


def ensure_plugins_installed(plugins: tuple[str, ...]) -> None:
    if not _have("uv"):
        raise SystemExit("uv is required to install plugins. https://docs.astral.sh/uv/")
    for name in plugins:
        plugin_dir = REPO_ROOT / "plugins" / name
        if not plugin_dir.is_dir():
            raise SystemExit(f"Plugin directory missing: {plugin_dir}")
        logger.info(f"Installing plugin: {name}")
        subprocess.run(
            ["uv", "pip", "install", "--python", sys.executable, "-e", str(plugin_dir)],
            check=True,
        )


def run_pyinstaller(plugins: tuple[str, ...]) -> Path:
    if not _have("pyinstaller"):
        raise SystemExit("pyinstaller not found. Run `uv sync --extra dev` first.")
    dist_dir = REPO_ROOT / "dist"
    build_dir = REPO_ROOT / "build"
    env = {**os.environ, "LOCAI_BUNDLE_PLUGINS": ",".join(plugins)}
    cmd = [
        "pyinstaller",
        "--noconfirm",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        str(SPEC_FILE),
    ]
    logger.info(f"Running: LOCAI_BUNDLE_PLUGINS={env['LOCAI_BUNDLE_PLUGINS']} {' '.join(cmd)}")
    subprocess.run(cmd, check=True, cwd=REPO_ROOT, env=env)
    return dist_dir / "locai-link"


def build_launcher() -> Path:
    """Compile the Rust launcher and return the path to the built binary.

    The launcher is the stable public entry point — it lives at
    ``<install_root>/locai-link`` and exec's whichever runtime version
    ``current`` points at. See ../OTA-BUNDLE.md §6.5.
    """
    if not _have("cargo"):
        raise SystemExit("cargo is required to build the launcher. Install Rust via https://rustup.rs/")
    if not LAUNCHER_DIR.is_dir():
        raise SystemExit(f"Launcher source missing at {LAUNCHER_DIR}")
    logger.info("Building launcher (cargo build --release)")
    subprocess.run(["cargo", "build", "--release", "-p", "locai-link-launcher"], cwd=CRATES_DIR, check=True)
    built = CARGO_TARGET_DIR / "release" / LAUNCHER_BINARY_NAME
    if not built.is_file():
        raise SystemExit(f"Launcher build did not produce {built}")
    return built


def install_launcher(install_root: Path, launcher_binary: Path) -> Path:
    """Copy the launcher binary into the install_root at its public name."""
    target = install_root / LAUNCHER_BINARY_NAME
    shutil.copy2(launcher_binary, target)
    # copy2 preserves perms but make sure it's executable.
    target.chmod(0o755)
    return target


def restructure_to_versioned_layout(bundle_dir: Path, version: str) -> Path:
    """Reshape PyInstaller's flat output into the install_root + versions/<v>/ layout.

    PyInstaller writes everything to ``<dist>/locai-link/``. We move that into
    ``versions/<version>/`` and add a ``current`` pointer so the tarball extracts
    on a target machine as a valid Pattern-A first install (see ../OTA-BUNDLE.md
    §4.1). Any stale ``versions``/``current``/``CURRENT`` from a prior build is
    removed first, else a second run would nest the old tree inside the new
    ``versions/<version>/``. Returns the new versioned bundle directory.
    """
    if not bundle_dir.is_dir():
        raise SystemExit(f"Expected PyInstaller output at {bundle_dir}, but it isn't a directory.")

    install_root = bundle_dir
    dist_root = install_root.parent
    staging = dist_root / f"_staged_{version}"

    # Clean stale versioning artefacts from a prior build — PyInstaller re-writes
    # everything else, but these live at the install-root layer we synthesise here.
    for stale in ("versions", "current", "CURRENT"):
        stale_path = install_root / stale
        if stale_path.is_symlink() or stale_path.is_file():
            stale_path.unlink()
        elif stale_path.is_dir():
            shutil.rmtree(stale_path)

    if staging.exists():
        shutil.rmtree(staging)
    install_root.rename(staging)

    versions_dir = install_root / "versions"
    versions_dir.mkdir(parents=True)
    target_dir = versions_dir / version
    staging.rename(target_dir)

    _write_current_pointer(install_root, version)
    return target_dir


def _write_current_pointer(install_root: Path, version: str) -> None:
    """Write the ``current`` pointer the launcher follows on start.

    POSIX: relative symlink ``current -> versions/<version>``. Windows hosts
    without Developer Mode / admin can't symlink, so fall back to a plain text
    ``CURRENT`` file holding the version. The launcher must accept both shapes.
    """
    rel_target = Path("versions") / version
    link = install_root / "current"
    if link.is_symlink() or link.exists():
        link.unlink()
    try:
        link.symlink_to(rel_target, target_is_directory=True)
        logger.info(f"  current -> {rel_target} (symlink)")
        return
    except (OSError, NotImplementedError) as exc:
        logger.warning(f"Symlink creation failed ({exc}); writing CURRENT pointer file instead.")
    pointer = install_root / "CURRENT"
    pointer.write_text(version + "\n", encoding="utf-8")
    logger.info(f"  CURRENT pointer file -> {version}")


def _read_root_version() -> str:
    pp = REPO_ROOT / "pyproject.toml"
    with pp.open("rb") as f:
        data = tomllib.load(f)
    return str(data["project"]["version"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--plugins",
        nargs="+",
        required=True,
        metavar="NAME",
        help=f"Plugins to compile into the bundle. Bundleable: {', '.join(KNOWN_PLUGINS)}.",
    )
    args = parser.parse_args()

    plugins = _resolve_plugins(args)
    tag = _platform_tag(_pf.system(), _pf.machine())
    asset_name = derive_asset_name(plugins)

    logger.info(f"== Bundle target: {tag} ==")
    logger.info(f"== Plugins: {', '.join(plugins)} ==")
    logger.info(f"== Asset name: {asset_name} ==")

    # Build the launcher first — if cargo/Rust isn't installed, fail fast
    # before the lengthy PyInstaller step.
    launcher_binary = build_launcher()

    run_prefetch(plugins, tag)
    ensure_plugins_installed(plugins)
    bundle_dir = run_pyinstaller(plugins)

    version = _read_root_version()
    logger.info(f"== Version: {version} ==")
    versioned_dir = restructure_to_versioned_layout(bundle_dir, version)
    manifest_path = write_manifest(versioned_dir, list(plugins), REPO_ROOT)

    install_root = bundle_dir
    launcher_path = install_launcher(install_root, launcher_binary)
    logger.info(f"Launcher installed: {launcher_path}")
    logger.info(f"Manifest written: {manifest_path}")
    logger.info(f"Install root: {install_root}")
    logger.info(f"  Versioned bundle: {versioned_dir}")
    logger.info(f"Test with: {launcher_path} --help")
    logger.info(
        f"Package the WHOLE install_root as: "
        f"{asset_name}-{tag}-v{version}.tar.gz (.zip on Windows). "
        "Extraction gives a valid Pattern-A first install."
    )


if __name__ == "__main__":
    main()
