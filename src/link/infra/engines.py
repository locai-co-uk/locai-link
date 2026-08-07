# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""On-demand engine provisioning.

The desktop / bundled build ships inference engines inside the runtime bundle;
the headless install ships none and fetches them from the artifact store at first
use. This module maps the engines the serving path needs to artifact-store
fetches under a writable per-install cache, verifies them, and returns the
directory the binary lives in. Idempotent and a no-op once an engine is present.

Each engine gets its own cache dir (its own verification marker), so llama-cpp
and llama-swap do not clobber each other's marker. The serving path resolves a
binary by asking for its engine here and then looking in the returned dir.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from link.infra import artifact_store

logger = logging.getLogger(__name__)


def _install_root(install_root: Path | None) -> Path:
    if install_root is not None:
        return install_root
    env = os.environ.get("LOCAI_INSTALL_ROOT")
    if env:
        return Path(env)
    # Lazy import keeps this module importable without the updater's dependencies.
    from link.app.updater import discover_install_root

    return discover_install_root()


def engine_cache_root(install_root: Path | None = None) -> Path:
    """Writable root the on-demand engines are cached under, one dir per engine."""
    return _install_root(install_root) / "engines"


def provision(
    name: str,
    *,
    version: str | None = None,
    install_root: Path | None = None,
    base: str | None = None,
) -> Path:
    """Ensure engine ``name`` is present + verified on this device, fetching it
    from the artifact store on demand, and return the directory its binary is in.
    ``version`` pins the fetch (plugins pass their vetted release constant so the
    store's default can never drift a device off the pin); None takes the store
    manifest's per-engine default. Idempotent."""
    dest = engine_cache_root(install_root) / name
    artifact_store.ensure_engine(name, version, dest_dir=dest, base=base)
    return dest


def binary_path(
    name: str,
    binary: str,
    *,
    version: str | None = None,
    install_root: Path | None = None,
    base: str | None = None,
) -> Path:
    """Provision engine ``name`` and return the full path to its server binary.

    ``binary`` is the server filename the calling plugin declares (already
    platform-resolved, e.g. ``llama-server`` / ``llama-server.exe``), so core
    holds no per-engine knowledge. Raises if it is not in the fetched archive."""
    bin_dir = provision(name, version=version, install_root=install_root, base=base)
    p = bin_dir / binary
    if p.exists():
        return p
    raise artifact_store.ArtifactStoreError(f"engine {name} provisioned but {binary} not found in {bin_dir}")
