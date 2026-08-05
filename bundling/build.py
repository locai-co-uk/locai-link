# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end Link bundle build.

A bundle is identified by the plugin set compiled into it. The artifact
name is derived from that plugin set (see ``bundling/manifest.py`` for
the codes and ordering). There is no separate "profile" concept; the
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

# Bundleable plugins: the keys of PLUGIN_CODES, in canonical order. Anything
# outside this set is a config error at parse time (a plugin without a code
# would produce an un-nameable artifact).
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
    transition. Small + harmless on platforms that never invoke it."""
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
        nargs="+",
        required=True,
        metavar="NAME",
        help=f"Plugins to compile into the bundle. Bundleable: {', '.join(KNOWN_PLUGINS)}.",
    )
    parser.add_argument(
        "--no-engines",
        action="store_true",
        help="Headless build: skip engine prefetch/bake. Engines are pulled on demand "
        "from the artifact store at first use, so the bundle ships without them.",
    )
    args = parser.parse_args()

    plugins = _resolve_plugins(args)
    tag = _platform_tag(_pf.system(), _pf.machine())
    asset_name = derive_asset_name(plugins)

    logger.info(f"== Bundle target: {tag} ==")
    logger.info(f"== Plugins: {', '.join(plugins)} ==")
    logger.info(f"== Asset name: {asset_name} ==")

    # build.py now produces only the runtime bundle (versions/ + current +
    # manifest). The single `locai-link` binary is the Tauri app build (the
    # supervisor + tray), staged into the install root by bundling (pack.sh /
    # the pkg staging), so there is no separate launcher to compile here.
    if args.no_engines:
        logger.info("== Headless build: skipping engine prefetch (engines fetched on demand) ==")
    else:
        run_prefetch(plugins, tag)
    ensure_plugins_installed(plugins)
    bundle_dir = run_pyinstaller(plugins)

    version = _read_root_version()
    logger.info(f"== Version: {version} ==")
    versioned_dir = restructure_to_versioned_layout(bundle_dir, version)
    _ship_migration_finisher(versioned_dir)
    manifest_path = write_manifest(versioned_dir, list(plugins), REPO_ROOT)

    install_root = bundle_dir
    logger.info(f"Manifest written: {manifest_path}")
    logger.info(f"Runtime bundle root: {install_root}")
    logger.info(f"  Versioned bundle: {versioned_dir}")
    logger.info(
        "Stage the `locai-link` app binary (cargo tauri build) into this root, "
        f"then package as {asset_name}-{tag}-v{version}.tar.gz (.zip on Windows)."
    )


if __name__ == "__main__":
    main()
