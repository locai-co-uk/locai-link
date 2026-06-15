# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Plugin code + asset-name conventions and manifest.json writer.

Asset name shape::

    locai-link-<plugin-codes>-<os>-<arch>-v<version>.<ext>

Where ``<plugin-codes>`` is the canonical-ordered, hyphen-joined codes for
the plugins compiled into the bundle. Examples::

    locai-link-llm-linux-x86_64-v1.0.14.tar.gz
    locai-link-llm-stt-linux-x86_64-v1.0.14.tar.gz

We only bundle plugins that have a code below. Bare (zero-plugin) bundles
aren't a thing — that's the source-install path (``curl … | bash``).
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


def derive_asset_name(plugins: list[str] | tuple[str, ...]) -> str:
    """Translate a plugin list into the canonical asset-name stem.

    Raises ``SystemExit`` on an empty list or any plugin with no code —
    bundling without a code is unsupported by design (forces the convention
    conversation when a new plugin is added).
    """
    if not plugins:
        raise SystemExit("No plugins selected — bare bundles aren't a release shape, use the source install path.")
    unknown = [p for p in plugins if p not in PLUGIN_CODES]
    if unknown:
        raise SystemExit(
            f"Plugins missing an asset-name code: {', '.join(unknown)}. "
            f"Add an entry to PLUGIN_CODES in bundling/manifest.py before bundling."
        )
    codes = [PLUGIN_CODES[p] for p in PLUGIN_ORDER if p in plugins]
    return "locai-link-" + "-".join(codes)


def write_manifest(
    bundle_dir: Path,
    plugins: list[str] | tuple[str, ...],
    repo_root: Path,
) -> Path:
    """Write ``manifest.json`` into the bundle root.

    Read-only metadata describing what was built. Not consumed by the
    running agent (that reads ``configs/agent.json``). Useful for bug
    reports, telemetry (agent can report the manifest at registration),
    and integrity checks.
    """
    asset_name = derive_asset_name(plugins)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "asset_name": asset_name,
        "version": _read_root_version(repo_root),
        "git_sha": _git_sha(repo_root),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plugins": [{"name": name, "version": _read_plugin_version(repo_root / "plugins" / name)} for name in plugins],
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
