# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end Link bundle build.

A bundle's shape (``desktop`` | ``headless``) drives the Rust feature + asset
name (see ``bundling/manifest.py``). Plugins (``--plugins``, omit for naked) and
engine delivery (``--prefetch`` = bundle engines in at build; without it they are
fetched on demand — same meaning for both shapes) are orthogonal choices.

Examples::

    uv run python bundling/build.py                                                 # naked headless, fetch-on-demand
    uv run python bundling/build.py --shape desktop --prefetch --plugins language_model audio_transcriber
    uv run python bundling/build.py --shape headless --plugins language_model --prefetch   # air-gapped headless

Steps:
    1. Resolve shape + plugin selection + engine policy.
    2. Pre-fetch native engines into the bundle when --prefetch is passed.
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
    SHAPES,
    asset_stem,
    platform_tag,
    write_manifest,
)
from prefetch import PREFETCHERS

SPEC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SPEC_DIR.parent
SPEC_FILE = SPEC_DIR / "locai-link.spec"

# Bundleable plugins: the keys of PLUGIN_CODES, in canonical order. A plugin
# outside this set is a config error (no code for the manifest plugin_set).
KNOWN_PLUGINS: list[str] = list(PLUGIN_CODES.keys())

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _resolve_plugins(args: argparse.Namespace) -> tuple[str, ...]:
    plugins = args.plugins or []
    unknown = [p for p in plugins if p not in KNOWN_PLUGINS]
    if unknown:
        raise SystemExit(f"Unknown plugins: {', '.join(unknown)}\nBundleable plugins: {', '.join(KNOWN_PLUGINS)}")
    # De-dupe + canonicalise via PLUGIN_CODES order. Empty = naked build.
    seen = set(plugins)
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


def run_pyinstaller(plugins: tuple[str, ...], prefetch: bool) -> Path:
    if not _have("pyinstaller"):
        raise SystemExit("pyinstaller not found. Run `uv sync --extra dev` first.")
    dist_dir = REPO_ROOT / "dist"
    build_dir = REPO_ROOT / "build"
    # LOCAI_BUNDLE_PREFETCH tells the spec whether to bake engine binaries into
    # the bundle. Without it, engines are fetched on demand, so a plugin can be
    # selected (its Python code ships) with no prefetched binaries present.
    env = {
        **os.environ,
        "LOCAI_BUNDLE_PLUGINS": ",".join(plugins),
        "LOCAI_BUNDLE_PREFETCH": "1" if prefetch else "0",
    }
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


def restructure_to_versioned_layout(bundle_dir: Path, version: str) -> Path:
    """Reshape PyInstaller's flat output into the install_root + versions/<v>/ layout.

    PyInstaller writes everything to ``<dist>/locai-link/``. We move that into
    ``versions/<version>/`` and add a ``current`` pointer so the tarball extracts
    on a target machine as a valid first install. Any stale
    ``versions``/``current``/``CURRENT`` from a prior build is removed first, else
    a second run would nest the old tree inside the new ``versions/<version>/``.
    Returns the new versioned bundle directory.
    """
    if not bundle_dir.is_dir():
        raise SystemExit(f"Expected PyInstaller output at {bundle_dir}, but it isn't a directory.")

    install_root = bundle_dir
    dist_root = install_root.parent
    staging = dist_root / f"_staged_{version}"

    # Clean stale versioning artefacts from a prior build; PyInstaller re-writes
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


def _ship_migration_finisher(versioned_dir: Path) -> None:
    """Ship the macOS migration finisher alongside the runtime so it is version-
    matched and present at ``<install_root>/current/finish-migration.sh`` after an
    OTA. The runtime runs it (via an admin prompt) to finish a pre-merge -> merged
    transition. Only staged for the macOS desktop build (see the call site)."""
    src = SPEC_DIR / "finish-migration.sh"
    if not src.is_file():
        logger.warning(f"migration finisher not found at {src}; skipping")
        return
    dst = versioned_dir / "finish-migration.sh"
    shutil.copy2(src, dst)
    dst.chmod(0o755)
    logger.info(f"  shipped migration finisher -> {dst}")


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
        nargs="*",
        default=[],
        metavar="NAME",
        help=f"Plugins to compile in (optional; omit for a naked build). Bundleable: {', '.join(KNOWN_PLUGINS)}.",
    )
    parser.add_argument(
        "--shape",
        choices=list(SHAPES),
        default="headless",
        help="Build shape: 'headless' (default; supervisor-only, no tray) or 'desktop' "
        "(tray/setup app). Drives the Rust feature + asset name. Engine delivery is a "
        "separate choice (see --prefetch).",
    )
    parser.add_argument(
        "--prefetch",
        action="store_true",
        help="Bundle the plugins' native engines into the build. Without it, engines are "
        "fetched from the artifact store on demand at first use. Same meaning for both "
        "shapes (releases: desktop --prefetch, headless without).",
    )
    args = parser.parse_args()

    plugins = _resolve_plugins(args)
    shape = args.shape
    prefetch = args.prefetch
    tag = platform_tag(_pf.system(), _pf.machine())
    asset_name = asset_stem(shape)

    logger.info(f"== Shape: {shape} ==")
    logger.info(f"== Bundle target: {tag} ==")
    logger.info(f"== Plugins: {', '.join(plugins) or '(none — naked)'} ==")
    logger.info(f"== Engines: {'bundled (prefetch)' if prefetch else 'fetched on demand'} ==")
    logger.info(f"== Asset name: {asset_name} ==")

    # build.py produces the runtime bundle (versions/ + current + manifest). The
    # single `locai-link` binary is the Rust build (feature per shape: ui for
    # desktop, --no-default-features for headless), staged into the install root
    # by pack.sh / the pkg staging.
    if prefetch:
        run_prefetch(plugins, tag)
    else:
        logger.info("== Engines fetched on demand from the artifact store (not bundled) ==")
    ensure_plugins_installed(plugins)
    bundle_dir = run_pyinstaller(plugins, prefetch)

    version = _read_root_version()
    logger.info(f"== Version: {version} ==")
    versioned_dir = restructure_to_versioned_layout(bundle_dir, version)
    # The migration finisher is macOS-desktop-only (pre-merge -> merged transition).
    # It has nothing to act on in a headless bundle or on Linux, so don't ship it there.
    if _pf.system() == "Darwin" and shape == "desktop":
        _ship_migration_finisher(versioned_dir)
    manifest_path = write_manifest(versioned_dir, list(plugins), REPO_ROOT, shape)

    install_root = bundle_dir
    logger.info(f"Manifest written: {manifest_path}")
    logger.info(f"Runtime bundle root: {install_root}")
    logger.info(f"  Versioned bundle: {versioned_dir}")
    rust_hint = "cargo tauri build" if shape == "desktop" else "cargo build --no-default-features"
    logger.info(
        f"Stage the `locai-link` {shape} binary ({rust_hint}) into this root, "
        f"then package as {asset_name}-{tag}-v{version}.tar.gz (.zip on Windows)."
    )


if __name__ == "__main__":
    main()
