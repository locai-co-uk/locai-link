# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Resolve the locai-link agent version (installed metadata or pyproject walk-up)."""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_cached: str | None = None


def resolve_agent_version() -> str | None:
    """Return the agent version, or ``None`` if it can't be determined.

    Resolution order:
        1. ``importlib.metadata`` (installed package).
        2. ``pyproject.toml`` walk-up from this file (editable installs).
        3. ``pyproject.toml`` walk-up from ``sys.argv[0]`` (frozen / script runs
           where ``__file__`` points deep into site-packages).

    Cached on success only — an early ``None`` (e.g. before ``sys.argv`` is
    populated) doesn't pin the result; a later call can still recover.
    """
    global _cached
    if _cached is not None:
        return _cached
    v = _resolve()
    if v is not None:
        _cached = v
    return v


def _resolve() -> str | None:
    try:
        v = version("locai-link")
        if v:
            return v
    except (PackageNotFoundError, Exception):
        pass

    try:
        import tomllib

        roots = [Path(__file__).resolve()]
        if getattr(sys, "argv", None) and sys.argv[0]:
            roots.append(Path(sys.argv[0]).resolve())

        for start in roots:
            for parent in [start, *start.parents]:
                candidate = parent / "pyproject.toml"
                if not candidate.is_file():
                    continue
                data = tomllib.loads(candidate.read_text(encoding="utf-8"))
                project = data.get("project") or {}
                if project.get("name") == "locai-link" and "version" in project:
                    return project["version"]
    except Exception:
        pass

    return None
