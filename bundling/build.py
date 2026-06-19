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
from pathlib import Path

from manifest import (  # type: ignore[import-not-found]
    PLUGIN_CODES,
    derive_asset_name,
    write_manifest,
)
from prefetch import PREFETCHERS, _platform_tag  # type: ignore[import-not-found]

SPEC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SPEC_DIR.parent
SPEC_FILE = SPEC_DIR / "locai-link.spec"

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

    run_prefetch(plugins, tag)
    ensure_plugins_installed(plugins)
    bundle_dir = run_pyinstaller(plugins)
    manifest_path = write_manifest(bundle_dir, list(plugins), REPO_ROOT)

    logger.info(f"Manifest written: {manifest_path}")
    logger.info(f"Bundle ready: {bundle_dir}")
    logger.info(f"Test with: {bundle_dir / 'locai-link'} --help")
    logger.info(
        f"Package as: {asset_name}-{tag}-v<version>.tar.gz (.zip on Windows). CI reads asset_name from manifest.json."
    )


if __name__ == "__main__":
    main()
