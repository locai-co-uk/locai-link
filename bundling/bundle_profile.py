# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import tomllib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


MANIFEST_VERSION = 1


@dataclass(frozen=True)
class BundleSpec:
    """The merged build recipe — what to compile, what to name it, what to stamp."""

    name: str
    display_name: str
    description: str
    asset_name: str
    plugins: tuple[str, ...] = field(default_factory=tuple)


def empty_spec() -> BundleSpec:
    """A spec with no profile loaded — used when CLI is the sole source.

    Names default to the canonical standalone identity; callers can override
    with --display-name / --description / --asset-name.
    """
    return BundleSpec(
        name="custom",
        display_name="Loc.ai Link",
        description="Custom bundle (no profile).",
        asset_name="locai-link",
        plugins=(),
    )


def load_profile(name: str, profiles_dir: Path) -> BundleSpec:
    """Load ``<profiles_dir>/<name>.yaml`` and validate basic shape.

    Raises ``SystemExit`` with a readable message on missing file, bad
    YAML, or missing required fields — this runs from a CLI, so SystemExit
    surfaces as a clean error message instead of a traceback.
    """
    path = profiles_dir / f"{name}.yaml"
    if not path.exists():
        # List available profiles so the typo case is obvious.
        available = sorted(p.stem for p in profiles_dir.glob("*.yaml"))
        raise SystemExit(
            f"Profile not found: {path}\nAvailable profiles in {profiles_dir}: {', '.join(available) or '(none)'}"
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise SystemExit(f"Profile {path} must be a YAML mapping at top level (got {type(raw).__name__})")

    return _spec_from_dict(raw, source=str(path))


def merge_cli(spec: BundleSpec, args: argparse.Namespace, known_plugins: list[str]) -> BundleSpec:
    """Layer CLI flag values on top of a (possibly empty) profile spec.

    A flag that is None / absent leaves the profile value alone. A flag with
    an explicit value wins. ``--all-plugins`` short-circuits to the full
    known set regardless of profile.
    """
    plugins = spec.plugins
    if getattr(args, "all_plugins", False):
        plugins = tuple(known_plugins)
    elif args.plugins:
        plugins = tuple(args.plugins)

    return replace(
        spec,
        plugins=plugins,
        asset_name=args.asset_name or spec.asset_name,
        display_name=args.display_name or spec.display_name,
        description=args.description or spec.description,
    )


def validate(spec: BundleSpec, known_plugins: list[str]) -> None:
    """Raise ``SystemExit`` on a malformed spec.

    Run after merge_cli so messages reflect the final inputs (a profile may
    have set a typo'd plugin, or a CLI override may have introduced one).
    """
    if not spec.asset_name:
        raise SystemExit("BundleSpec: asset_name is required (pass --asset-name or set it in the profile)")
    if not all(c.isalnum() or c in "-_" for c in spec.asset_name):
        raise SystemExit(
            f"BundleSpec: asset_name {spec.asset_name!r} must contain only [a-zA-Z0-9-_]; "
            "it becomes part of a filename."
        )
    unknown = [p for p in spec.plugins if p not in known_plugins]
    if unknown:
        raise SystemExit(
            f"Unknown plugins in profile/CLI: {', '.join(unknown)}\nKnown plugins: {', '.join(known_plugins)}"
        )


# ---------------------------------------------------------------------------
# Manifest writing
# ---------------------------------------------------------------------------


def write_manifest(bundle_dir: Path, spec: BundleSpec, repo_root: Path) -> Path:
    """Write ``manifest.json`` into the bundle root.

    Manifest is read-only metadata describing what was built. Not consumed
    by the running agent — that reads ``configs/agent.json``. Useful for
    bug reports, telemetry (agent reports profile during registration),
    and integrity checks.
    """
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "profile": spec.name,
        "display_name": spec.display_name,
        "description": spec.description,
        "version": _read_root_version(repo_root),
        "git_sha": _git_sha(repo_root),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asset_name": spec.asset_name,
        "plugins": [
            {"name": name, "version": _read_plugin_version(repo_root / "plugins" / name)} for name in spec.plugins
        ],
    }
    target = bundle_dir / "manifest.json"
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_REQUIRED_FIELDS = ("name", "display_name", "description", "asset_name")
_OPTIONAL_FIELDS = ("plugins",)
_KNOWN_FIELDS = set(_REQUIRED_FIELDS + _OPTIONAL_FIELDS)


def _spec_from_dict(raw: dict, source: str) -> BundleSpec:
    """Parse a YAML-loaded dict into a BundleSpec.

    Strict on unknown top-level fields — catches typos like ``pluginz:``
    that would otherwise silently produce an empty plugin list.
    """
    extra = set(raw.keys()) - _KNOWN_FIELDS
    if extra:
        raise SystemExit(
            f"Profile {source}: unknown top-level field(s): {', '.join(sorted(extra))}\n"
            f"Known fields: {', '.join(sorted(_KNOWN_FIELDS))}"
        )

    missing = [f for f in _REQUIRED_FIELDS if f not in raw]
    if missing:
        raise SystemExit(f"Profile {source}: missing required field(s): {', '.join(missing)}")

    plugins = raw.get("plugins") or []
    if not isinstance(plugins, list) or not all(isinstance(p, str) for p in plugins):
        raise SystemExit(f"Profile {source}: 'plugins' must be a list of strings, got {plugins!r}")

    return BundleSpec(
        name=str(raw["name"]),
        display_name=str(raw["display_name"]),
        description=str(raw["description"]),
        asset_name=str(raw["asset_name"]),
        plugins=tuple(plugins),
    )


def _git_sha(repo_root: Path) -> str:
    """Short git SHA of the working tree; 'unknown' off a git repo."""
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
    """Read [project].version from the root pyproject.toml."""
    pp = repo_root / "pyproject.toml"
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
        return str(data.get("project", {}).get("version", "0.0.0"))
    except (OSError, tomllib.TOMLDecodeError):
        return "0.0.0"


def _read_plugin_version(plugin_dir: Path) -> str:
    """Read [project].version from a plugin's pyproject.toml."""
    pp = plugin_dir / "pyproject.toml"
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
        return str(data.get("project", {}).get("version", "0.0.0"))
    except (OSError, tomllib.TOMLDecodeError):
        return "0.0.0"
