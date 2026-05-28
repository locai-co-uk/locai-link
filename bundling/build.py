# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end Link bundle build.

The caller selects which plugins are part of the bundle.  Different partners
need different inference shapes — Meetily wants `language_model` and
`audio_transcriber`; another integration may want only the LLM.  Plugins not
listed are not installed and their dist-info is not collected.

Steps:
    1. Pre-fetch native binaries needed by the selected plugins.
    2. Editable-install each selected plugin into the active venv so its
       dist-info is available to PyInstaller's copy_metadata().
    3. Invoke PyInstaller against locai-link.spec, passing the plugin list
       via the LOCAI_BUNDLE_PLUGINS env var.
    4. Bundle lands under dist/locai-link/.

Examples:
    uv run python bundling/build.py --plugins language_model audio_transcriber
    uv run python bundling/build.py --all-plugins
"""

import argparse
import logging
import os
import platform as _pf
import shutil
import subprocess
import sys
from pathlib import Path

from prefetch import PREFETCHERS, _platform_tag  # type: ignore[import-not-found]

SPEC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SPEC_DIR.parent
SPEC_FILE = SPEC_DIR / "locai-link.spec"

# Plugins this bundler knows how to include.  A plugin appearing in
# bundling.prefetch.PREFETCHERS has native binaries that must be staged before
# PyInstaller runs; the rest are pure Python.
KNOWN_PLUGINS: list[str] = [
    "language_model",
    "audio_transcriber",
    "audio_classifier",
    "image_classifier",
]

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _resolve_plugins(args: argparse.Namespace) -> list[str]:
    if args.all_plugins:
        return list(KNOWN_PLUGINS)
    if not args.plugins:
        raise SystemExit(
            "No plugins selected. Pass --plugins <name> [<name> ...] or --all-plugins.\n"
            f"Known plugins: {', '.join(KNOWN_PLUGINS)}"
        )
    unknown = [p for p in args.plugins if p not in KNOWN_PLUGINS]
    if unknown:
        raise SystemExit(f"Unknown plugins: {', '.join(unknown)}\nKnown plugins: {', '.join(KNOWN_PLUGINS)}")
    return list(args.plugins)


def run_prefetch(plugins: list[str], tag: str) -> None:
    """Stage native binaries for whichever selected plugins need them."""
    artifacts_root = SPEC_DIR / "_artifacts" / tag
    for name in plugins:
        prefetcher = PREFETCHERS.get(name)
        if prefetcher is None:
            continue  # pure-Python plugin, nothing to stage
        logger.info(f"Pre-fetching native binaries for {name}")
        prefetcher(artifacts_root)


def ensure_plugins_installed(plugins: list[str]) -> None:
    """Editable-install each selected plugin into the active venv."""
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


def run_pyinstaller(plugins: list[str]) -> Path:
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
        metavar="NAME",
        help=f"Plugins to include. Known: {', '.join(KNOWN_PLUGINS)}.",
    )
    parser.add_argument(
        "--all-plugins",
        action="store_true",
        help="Include every known plugin. Prefer an explicit list for partner bundles.",
    )
    args = parser.parse_args()

    plugins = _resolve_plugins(args)
    tag = _platform_tag(_pf.system(), _pf.machine())

    logger.info(f"== Bundle target: {tag} ==")
    logger.info(f"== Plugins: {', '.join(plugins)} ==")

    run_prefetch(plugins, tag)
    ensure_plugins_installed(plugins)
    out = run_pyinstaller(plugins)

    logger.info(f"Bundle ready: {out}")
    logger.info(f"Test with: {out / 'locai-link'} --help")


if __name__ == "__main__":
    main()
