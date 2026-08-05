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

from link.app import artifact_store

logger = logging.getLogger(__name__)

# The server binary each engine provides, per platform. The serving path looks
# for this name inside the provisioned dir.
ENGINE_BINARY = {
    "llama-cpp": ("llama-server.exe", "llama-server"),
    "llama-swap": ("llama-swap.exe", "llama-swap"),
    "whisper-cpp": ("whisper-server.exe", "whisper-server"),
}


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


def provision(name: str, *, install_root: Path | None = None, base: str | None = None) -> Path:
    """Ensure engine ``name`` is present + verified on this device, fetching it
    from the artifact store on demand, and return the directory its binary is in.
    Version is the store manifest's per-engine default. Idempotent."""
    dest = engine_cache_root(install_root) / name
    artifact_store.ensure_engine(name, dest_dir=dest, base=base)
    return dest


def binary_path(name: str, *, install_root: Path | None = None, base: str | None = None) -> Path:
    """Provision engine ``name`` and return the full path to its server binary.
    Raises if the expected binary is not in the fetched archive."""
    win, unix = ENGINE_BINARY[name]
    bin_dir = provision(name, install_root=install_root, base=base)
    for candidate in (unix, win):
        p = bin_dir / candidate
        if p.exists():
            return p
    raise artifact_store.ArtifactStoreError(f"engine {name} provisioned but no server binary found in {bin_dir}")
