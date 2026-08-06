# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Shape/asset-name conventions and manifest.json writer.

Asset name shape::

    locai-link-<shape>-<os>-<arch>-v<version>.<ext>

Where ``<shape>`` is the build shape (``desktop`` | ``headless``), ``<os>`` is
``macos``/``linux``/``windows`` and ``<arch>`` is ``x64``/``arm64``. Examples::

    locai-link-desktop-macos-arm64-v1.3.0.pkg
    locai-link-headless-linux-x64-v1.3.0.tar.gz

The plugin set is recorded inside ``manifest.json`` (not the filename); the
standard set is fixed per release, so shape is the naming axis. ``PLUGIN_CODES``
below stay for that metadata + boot.json's ``plugin_set``.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_VERSION = 1

# Plugin → short code. Canonical naming for the artifact filename.
# Add a code here before bundling a new plugin; the build hard-fails otherwise.
PLUGIN_CODES: dict[str, str] = {
    "language_model": "llm",
    "audio_transcriber": "stt",
}

# Canonical order plugin codes appear in the asset name. Headline-first;
# documented so two CI runs of the same plugin set produce identical names
# regardless of how the operator typed the --plugins list.
PLUGIN_ORDER: tuple[str, ...] = (
    "language_model",
    "audio_transcriber",
)


# Build shapes. `desktop` = tray/setup app (ui feature, engines baked);
# `headless` = supervisor-only (no ui, engines fetched on demand).
SHAPES: tuple[str, ...] = ("desktop", "headless")


def asset_stem(shape: str) -> str:
    """Canonical asset-name stem for a build shape: ``locai-link-<shape>``."""
    if shape not in SHAPES:
        raise SystemExit(f"Unknown shape {shape!r}; expected one of {', '.join(SHAPES)}.")
    return f"locai-link-{shape}"


_ARCH_TAGS = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}
_OS_TAGS = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}


def platform_tag(os_name: str, machine: str) -> str:
    """Canonical ``<os>-<arch>`` asset segment. ``arch`` is ``x64``/``arm64``
    (unified with the engine store + headless installer). The single source of
    truth for the platform tag — release.yml, pack.sh, prefetch, and the OTA
    resolver (updater._platform_tag) all agree with this. Rejects unknown
    arch/os so a build can't mislabel (e.g. armv7l as x64) and ship an
    unrunnable bundle."""
    arch = _ARCH_TAGS.get(machine.lower())
    if arch is None:
        raise SystemExit(f"Unsupported architecture: {machine!r} (expected {', '.join(sorted(_ARCH_TAGS))}).")
    os_slug = _OS_TAGS.get(os_name)
    if os_slug is None:
        raise SystemExit(f"Unsupported OS: {os_name!r} (expected {', '.join(_OS_TAGS)}).")
    return f"{os_slug}-{arch}"


def write_manifest(
    bundle_dir: Path,
    plugins: list[str] | tuple[str, ...],
    repo_root: Path,
    shape: str,
) -> Path:
    """Write ``manifest.json`` into the bundle root.

    Read-only metadata describing what was built. Not consumed by the
    running agent (that reads ``configs/agent.json``). ``asset_name`` is the
    shape-based OTA stem (updater resolves ``<asset_name>-<platform_tag>-v<ver>``).
    The plugin set is recorded here, not in the filename.
    """
    # Canonical ordering so two CI runs with the plugins passed in different
    # orders produce byte-identical manifest.json.
    canonical = [p for p in PLUGIN_ORDER if p in set(plugins)]
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "asset_name": asset_stem(shape),
        "shape": shape,
        "version": _read_root_version(repo_root),
        "git_sha": _git_sha(repo_root),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plugins": [
            {"name": name, "version": _read_plugin_version(repo_root / "plugins" / name)} for name in canonical
        ],
    }
    target = bundle_dir / "manifest.json"
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _git_sha(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except (subprocess.SubprocessError, FileNotFoundError):
        return "unknown"


def _read_root_version(repo_root: Path) -> str:
    pp = repo_root / "pyproject.toml"
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
        return str(data.get("project", {}).get("version", "0.0.0"))
    except (OSError, tomllib.TOMLDecodeError):
        return "0.0.0"


def _read_plugin_version(plugin_dir: Path) -> str:
    pp = plugin_dir / "pyproject.toml"
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
        return str(data.get("project", {}).get("version", "0.0.0"))
    except (OSError, tomllib.TOMLDecodeError):
        return "0.0.0"
