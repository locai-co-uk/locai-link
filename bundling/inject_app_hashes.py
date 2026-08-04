# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Inject UI-app content hashes into a bundle ``manifest.json`` for whole-app OTA.

``swap_bundle`` compares these hashes between the installed and the new manifest
and re-swaps a UI app only when its hash changed. We hash each app's
*source* (its crate + the shared crate + the lockfile), not the built binary:
release builds aren't byte-reproducible, so a source hash is what stays stable
when nothing actually changed, which is what makes the "only swap if changed"
behaviour work.

Usage::

    python3 bundling/inject_app_hashes.py --manifest <path> --repo-root <root>
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

# What each app's binary is built from. Source-based so an unchanged app keeps a
# stable hash across non-reproducible builds. Keep in sync with the crates that
# actually feed each Tauri binary.
APP_SOURCES: dict[str, list[str]] = {
    "companion": ["crates/link", "crates/Cargo.lock", "crates/Cargo.toml"],
}

# Build/output dirs to skip when walking source trees (huge + not source).
_SKIP_DIRS = {"target", "node_modules", "dist", ".svelte-kit", "gen", "__pycache__"}


def _hash_sources(repo_root: Path, rels: list[str]) -> str:
    files: list[Path] = []
    for rel in rels:
        p = repo_root / rel
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                files.extend(Path(dirpath) / fn for fn in filenames)

    h = hashlib.sha256()
    for f in sorted(files, key=lambda x: str(x.relative_to(repo_root))):
        h.update(str(f.relative_to(repo_root)).encode("utf-8"))
        h.update(b"\0")
        h.update(f.read_bytes())
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="path to the bundle manifest.json")
    parser.add_argument("--repo-root", default=".", help="repo root the source paths resolve against")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["apps"] = {app: _hash_sources(repo_root, rels) for app, rels in APP_SOURCES.items()}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"injected app hashes into {manifest_path}: {manifest['apps']}")


if __name__ == "__main__":
    main()
