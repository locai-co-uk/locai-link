# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Publish engine artifacts into the Link artifact-store layout.

Lays a source archive at ``{capability}/{name}/{version}/{platform-arch}/{file}``
with a ``.sha256`` sibling, then rebuilds ``index/manifest.v1.json`` (the only
mutable object) as the atomic commit point: the immutable version dir goes up
first, the manifest flips last.

Two modes:

* ``--from-dir`` seeds a store from archives already on disk. This is how the
  local mock store for 460 testing is built (before 459 stands up the real
  bucket + CI).
* ``--from-upstream`` downloads the pinned engine releases and repackages them.
  That is the 459 CI publish job; the per-engine / per-platform asset recipe
  below is its source of truth.

The store client (``link.infra.artifact_store``) is the reader for what this writes.

whisper-cpp has no upstream *server* prebuilt for macOS (only an xcframework),
so darwin variants must be built and dropped in via ``--from-dir`` until the
459 job grows a build step; they are intentionally absent from the upstream map.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, TypedDict

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

MANIFEST_SCHEMA = 1
MANIFEST_REL = "index/manifest.v1.json"
CAPABILITY_ENGINES = "engines"

# Pinned engine versions — keep in step with the plugin installers
# (plugins/language_model/install.py, plugins/audio_transcriber/install.py).
ENGINE_VERSIONS = {
    "llama-cpp": "b10289",
    "llama-swap": "247",
    "whisper-cpp": "v1.9.2",
}

# Upstream asset recipe: how to build each store variant from an upstream release
# asset. {name: {version-fmt-url-base, variant: asset-filename}}. The {tag}
# placeholder is the pinned version. The variant key is <platform-arch>[-<accel>];
# the cpu/portable build has no accel suffix and is the client's final fallback.
# The client (artifact_store.variant_candidates) prefers an accel build when the
# device has the hardware and the manifest carries it, else degrades to cpu.
class _EngineSpec(TypedDict):
    base: str  # release URL base with a {tag} placeholder
    assets: dict[str, str]  # variant -> upstream asset filename ({tag} placeholder)


UPSTREAM: dict[str, _EngineSpec] = {
    "llama-cpp": {
        "base": "https://github.com/ggml-org/llama.cpp/releases/download/{tag}",
        "assets": {
            "linux-x64": "llama-{tag}-bin-ubuntu-x64.tar.gz",
            "linux-x64-vulkan": "llama-{tag}-bin-ubuntu-vulkan-x64.tar.gz",
            "linux-arm64": "llama-{tag}-bin-ubuntu-arm64.tar.gz",
            "macos-x64": "llama-{tag}-bin-macos-x64.tar.gz",
            "macos-arm64": "llama-{tag}-bin-macos-arm64.tar.gz",
            "windows-x64": "llama-{tag}-bin-win-cpu-x64.zip",
            "windows-x64-vulkan": "llama-{tag}-bin-win-vulkan-x64.zip",
            "windows-x64-cuda-12.4": "llama-{tag}-bin-win-cuda-12.4-x64.zip",
            "windows-x64-cuda-13.3": "llama-{tag}-bin-win-cuda-13.3-x64.zip",
        },
    },
    "llama-swap": {
        "base": "https://github.com/mostlygeek/llama-swap/releases/download/v{tag}",
        "assets": {
            "linux-x64": "llama-swap_{tag}_linux_amd64.tar.gz",
            "linux-arm64": "llama-swap_{tag}_linux_arm64.tar.gz",
            "macos-x64": "llama-swap_{tag}_darwin_amd64.tar.gz",
            "macos-arm64": "llama-swap_{tag}_darwin_arm64.tar.gz",
            "windows-x64": "llama-swap_{tag}_windows_amd64.zip",
        },
    },
    "whisper-cpp": {
        "base": "https://github.com/ggml-org/whisper.cpp/releases/download/{tag}",
        "assets": {
            # v1.9.2 added Linux server prebuilts; Windows uses the BLAS build.
            # darwin has no upstream server binary -> --from-dir until 459 builds it.
            "linux-x64": "whisper-bin-ubuntu-x64.tar.gz",
            "linux-arm64": "whisper-bin-ubuntu-arm64.tar.gz",
            "windows-x64": "whisper-blas-bin-x64.zip",
        },
    },
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _store_filename(name: str, version: str, arch: str, ext: str) -> str:
    """Uniform store filename, independent of the upstream asset name."""
    return f"{name}-{version}-{arch}{ext}"


def place_artifact(store_dir: Path, capability: str, name: str, version: str, arch: str, archive: Path) -> str:
    """Copy ``archive`` into the store layout with a uniform name + a .sha256
    sibling. Returns the store-relative path. Version dirs are write-once, so a
    re-publish of the same variant overwrites in place."""
    ext = "".join(archive.suffixes[-2:]) if archive.name.endswith(".tar.gz") else archive.suffix
    fname = _store_filename(name, version, arch, ext)
    rel = f"{capability}/{name}/{version}/{arch}/{fname}"
    dest = store_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, dest)
    digest = _sha256(dest)
    (dest.with_name(dest.name + ".sha256")).write_text(f"{digest}  {fname}\n", encoding="utf-8")
    logger.info(f"placed {rel} ({digest[:12]}…)")
    return rel


def rebuild_manifest(store_dir: Path) -> Path:
    """Scan the store tree and (re)write ``index/manifest.v1.json`` atomically.
    The manifest is derived from what is on disk, so it can never drift from the
    artifacts. Written via a temp + os.replace so a reader never sees a partial."""
    manifest: dict[str, Any] = {"schema": MANIFEST_SCHEMA}
    for cap_dir in sorted(p for p in store_dir.iterdir() if p.is_dir() and p.name != "index"):
        for name_dir in sorted(p for p in cap_dir.iterdir() if p.is_dir()):
            for ver_dir in sorted(p for p in name_dir.iterdir() if p.is_dir()):
                for arch_dir in sorted(p for p in ver_dir.iterdir() if p.is_dir()):
                    archives = [f for f in arch_dir.iterdir() if f.is_file() and not f.name.endswith(".sha256")]
                    if not archives:
                        continue
                    art = archives[0]
                    rel = f"{cap_dir.name}/{name_dir.name}/{ver_dir.name}/{arch_dir.name}/{art.name}"
                    manifest.setdefault(cap_dir.name, {}).setdefault(name_dir.name, {}).setdefault(ver_dir.name, {})[
                        arch_dir.name
                    ] = {"path": rel, "sha256": _sha256(art), "size": art.stat().st_size}
    # Per-engine default version, so a device resolves what to fetch from the
    # manifest instead of re-pinning versions in the runtime. Prefer the pinned
    # The default version MUST be the ENGINE_VERSIONS pin — never guessed. A
    # lexical sort of build tags is wrong (b9999 > b10000), so a missing/unmatched
    # pin is a publish bug that would ship a stale engine as the default.
    defaults: dict[str, str] = {}
    for name, versions in manifest.get(CAPABILITY_ENGINES, {}).items():
        pin = ENGINE_VERSIONS.get(name)
        if pin is None or pin not in versions:
            raise SystemExit(f"no pinned ENGINE_VERSIONS entry for engine {name!r} (present: {sorted(versions)})")
        defaults[name] = pin
    if defaults:
        manifest["defaults"] = {CAPABILITY_ENGINES: defaults}

    out = store_dir / MANIFEST_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.new")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, out)
    logger.info(f"wrote {MANIFEST_REL}")
    return out


def _from_dir(store_dir: Path, source: Path) -> None:
    """Seed the store from a flat dir of archives named
    ``{name}-{version}-{platform-arch}.<ext>`` (the mock-store convention)."""
    for archive in sorted(source.iterdir()):
        if archive.name.endswith(".sha256") or not archive.is_file():
            continue
        stem = archive.name
        for ext in (".tar.gz", ".tgz", ".zip"):
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        parts = stem.split("-")
        # name may contain hyphens (llama-cpp) and the platform tuple may carry a
        # variant suffix (linux-x64-vulkan), so anchor on the platform token:
        # everything from it is platform-arch[-variant], the token before it is
        # the version, the rest is the name.
        plat_idx = next((i for i, p in enumerate(parts) if p in ("linux", "macos", "windows")), None)
        if plat_idx is None or plat_idx < 2:
            raise SystemExit(
                f"unrecognised artifact name {archive.name!r}: "
                "expected {name}-{version}-{platform-arch[-variant]}"
            )
        arch = "-".join(parts[plat_idx:])
        version = parts[plat_idx - 1]
        name = "-".join(parts[: plat_idx - 1])
        place_artifact(store_dir, CAPABILITY_ENGINES, name, version, arch, archive)
    rebuild_manifest(store_dir)


def _from_upstream(store_dir: Path) -> None:
    """Download the pinned engine releases and repackage them into the store.
    This is the 459 CI job; kept minimal here (no signing, no GPU variants, no
    whisper-macOS build). Best-effort per variant so one 404 doesn't abort all."""
    import urllib.request

    for name, spec in UPSTREAM.items():
        tag = ENGINE_VERSIONS[name]
        base = spec["base"].format(tag=tag)
        for arch, asset_fmt in spec["assets"].items():
            asset = asset_fmt.format(tag=tag)
            url = f"{base}/{asset}"
            tmp = store_dir / ".download" / asset
            tmp.parent.mkdir(parents=True, exist_ok=True)
            try:
                logger.info(f"downloading {url}")
                with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as fh:  # noqa: S310
                    shutil.copyfileobj(resp, fh)
                place_artifact(store_dir, CAPABILITY_ENGINES, name, tag, arch, tmp)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"skip {name}/{arch}: {e}")
            finally:
                tmp.unlink(missing_ok=True)
        shutil.rmtree(store_dir / ".download", ignore_errors=True)
    rebuild_manifest(store_dir)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", required=True, type=Path, help="store root dir (local mock or a GCS mount)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-dir", type=Path, help="seed from a flat dir of {name}-{version}-{arch}.<ext> archives")
    src.add_argument("--from-upstream", action="store_true", help="download + repackage the pinned upstream releases")
    ap.add_argument("--manifest-only", action="store_true", help="just rebuild index/manifest.v1.json from the store")
    args = ap.parse_args()

    args.store.mkdir(parents=True, exist_ok=True)
    if args.manifest_only:
        rebuild_manifest(args.store)
    elif args.from_dir:
        _from_dir(args.store, args.from_dir)
    else:
        _from_upstream(args.store)


if __name__ == "__main__":
    main()
