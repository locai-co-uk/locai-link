# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end Link bundle build.

A *profile* (``bundling/profiles/<name>.yaml``) is the canonical recipe:
which plugins to compile in, what to name the artifact, what to stamp
into ``manifest.json``. CLI flags can override individual fields or
build from scratch with no profile at all.

Examples::
    # Profile-driven (the reproducible release path)
    uv run python bundling/build.py --profile meetily

    # Profile + override (debug a single field)
    uv run python bundling/build.py --profile safechat --asset-name locai-link-test

    # Pure CLI (ad-hoc, no committed profile)
    uv run python bundling/build.py --plugins language_model --asset-name locai-link-x
"""

import argparse
import logging
import os
import platform as _pf
import shutil
import subprocess
import sys
from pathlib import Path

from bundle_profile import (  # type: ignore[import-not-found]
    BundleSpec,
    empty_spec,
    load_profile,
    merge_cli,
    validate,
    write_manifest,
)
from prefetch import PREFETCHERS, _platform_tag  # type: ignore[import-not-found]

SPEC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SPEC_DIR.parent
SPEC_FILE = SPEC_DIR / "locai-link.spec"
PROFILES_DIR = SPEC_DIR / "profiles"

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


def _resolve_spec(args: argparse.Namespace) -> BundleSpec:
    """Profile (if any) + CLI overrides → final BundleSpec."""
    spec = load_profile(args.profile, PROFILES_DIR) if args.profile else empty_spec()
    spec = merge_cli(spec, args, KNOWN_PLUGINS)
    if not spec.plugins:
        raise SystemExit(
            "No plugins selected.\n"
            "Pass --profile <name>, --plugins <name> [<name> ...], or --all-plugins.\n"
            f"Known plugins: {', '.join(KNOWN_PLUGINS)}\n"
            f"Available profiles: {', '.join(sorted(p.stem for p in PROFILES_DIR.glob('*.yaml')))}"
        )
    validate(spec, KNOWN_PLUGINS)
    return spec


def run_prefetch(plugins: tuple[str, ...], tag: str) -> None:
    """Stage native binaries for whichever selected plugins need them."""
    artifacts_root = SPEC_DIR / "_artifacts" / tag
    for name in plugins:
        prefetcher = PREFETCHERS.get(name)
        if prefetcher is None:
            continue  # pure-Python plugin, nothing to stage
        logger.info(f"Pre-fetching native binaries for {name}")
        prefetcher(artifacts_root)


def ensure_plugins_installed(plugins: tuple[str, ...]) -> None:
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
        "--profile",
        metavar="NAME",
        help=(
            "Load bundling/profiles/<NAME>.yaml as the base recipe. "
            "CLI flags below override profile values. "
            f"Available: {', '.join(sorted(p.stem for p in PROFILES_DIR.glob('*.yaml'))) or '(none)'}"
        ),
    )

    plugin_group = parser.add_mutually_exclusive_group()
    plugin_group.add_argument(
        "--plugins",
        nargs="+",
        metavar="NAME",
        help=f"Plugins to include. Known: {', '.join(KNOWN_PLUGINS)}. Overrides --profile.",
    )
    plugin_group.add_argument(
        "--all-plugins",
        action="store_true",
        help="Include every known plugin. Rarely what you want — see PROFILES.md.",
    )

    parser.add_argument(
        "--asset-name",
        metavar="NAME",
        default=None,
        help="Artifact name prefix (becomes part of the release filename). Overrides --profile.",
    )
    parser.add_argument(
        "--display-name",
        metavar="TEXT",
        default=None,
        help="Human-readable name for manifest.json. Overrides --profile.",
    )
    parser.add_argument(
        "--description",
        metavar="TEXT",
        default=None,
        help="Free-text description for manifest.json. Overrides --profile.",
    )

    args = parser.parse_args()

    spec = _resolve_spec(args)
    tag = _platform_tag(_pf.system(), _pf.machine())

    logger.info(f"== Bundle target: {tag} ==")
    logger.info(f"== Profile: {spec.name} ({spec.display_name}) ==")
    logger.info(f"== Plugins: {', '.join(spec.plugins)} ==")
    logger.info(f"== Asset name: {spec.asset_name} ==")

    run_prefetch(spec.plugins, tag)
    ensure_plugins_installed(spec.plugins)
    bundle_dir = run_pyinstaller(spec.plugins)
    manifest_path = write_manifest(bundle_dir, spec, REPO_ROOT)

    logger.info(f"Manifest written: {manifest_path}")
    logger.info(f"Bundle ready: {bundle_dir}")
    logger.info(f"Test with: {bundle_dir / 'locai-link'} --help")
    logger.info(
        f"Package as: {spec.asset_name}-{tag}-<version>.tar.gz "
        "(or .zip on Windows) — CI reads asset_name from manifest.json."
    )


if __name__ == "__main__":
    main()
