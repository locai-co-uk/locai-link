# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Client for the Link artifact store: engines, tools, and framework sidecars.

The store is a dumb, path-addressed, immutable public CDN. The device computes
an artifact's URL from its own hardware detection, GETs it over HTTPS, and
verifies the sha256 recorded in a signed manifest. Models are NOT served here:
they flow through Control per-device, so this client only ever touches engines
and tooling, never customer-entitled data.

Layout on the store::

    index / manifest.v1.json  # the only mutable object
    {capability} / {name} / {version} / {platform - arch} / {file}
    engines / llama - cpp / b10289 / linux - x64 / llama - cpp - b10289 - linux - x64.tar.gz

The manifest maps capability -> name -> version -> platform-arch -> a variant
record ({path, sha256, size}). ``path`` is relative to the store base, so a
device resolves a full URL from its own detection with no server round-trip
beyond reading the manifest.

Base URL comes from ``LOCAI_ARTIFACT_BASE`` when set (point it at a local mock
store for testing), else the production CDN. The bundled/desktop build still
ships engines inline; this path is for the headless install, which fetches them
on demand at first use.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Production CDN host, so device URLs never carry the bucket name. Overridable
# via env for a local mock store (and for the -dev/staging siblings).
DEFAULT_BASE = "https://storage.googleapis.com/locai-platform-artifacts-prod"
_BASE_ENV = "LOCAI_ARTIFACT_BASE"
MANIFEST_PATH = "index/manifest.v1.json"

CAPABILITY_ENGINES = "engines"

_DOWNLOAD_TIMEOUT = 60  # seconds per attempt
_MANIFEST_TIMEOUT = 15
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024  # a manifest is small; cap defensively
_HASH_CHUNK = 1024 * 1024


class ArtifactStoreError(Exception):
    """Base for artifact-store failures."""


class ManifestError(ArtifactStoreError):
    """Manifest missing, unreadable, or the wrong schema."""


class VariantNotFound(ArtifactStoreError):
    """No artifact for the requested capability/name/version/platform-arch."""


class VerificationError(ArtifactStoreError):
    """Downloaded bytes did not match the manifest sha256."""


def base_url() -> str:
    """Store base URL, ``LOCAI_ARTIFACT_BASE`` override winning. No trailing slash."""
    return (os.environ.get(_BASE_ENV) or DEFAULT_BASE).rstrip("/")


def platform_arch() -> str:
    """This device's ``<platform>-<arch>`` token, matching the store layout
    (``linux-x64``, ``linux-arm64``, ``darwin-arm64``, ``darwin-x64``,
    ``windows-x64``). Raises for an unmapped platform/arch so a wrong guess never
    resolves to a bogus URL."""
    if sys.platform.startswith("linux"):
        system = "linux"
    elif sys.platform == "darwin":
        system = "macos"
    elif sys.platform in ("win32", "cygwin"):
        system = "windows"
    else:
        raise ArtifactStoreError(f"unsupported platform: {sys.platform}")
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "x64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        raise ArtifactStoreError(f"unsupported architecture: {machine}")
    return f"{system}-{arch}"


def _cuda_major() -> int | None:
    """CUDA major version from nvcc or nvidia-smi, or None. Best-effort."""
    for cmd, pat in ((["nvcc", "--version"], r"release\s+(\d+)\."), (["nvidia-smi"], r"CUDA Version:\s*(\d+)\.")):
        if shutil.which(cmd[0]) is None:
            continue
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
            m = re.search(pat, out)
            if m:
                return int(m.group(1))
        except Exception as exc:  # noqa: BLE001
            # A failed probe just means CPU fallback; log why for diagnosability.
            logger.debug(f"CUDA probe {cmd[0]} failed: {exc}")
    return None


def _has_gpu() -> bool:
    return shutil.which("nvidia-smi") is not None or shutil.which("rocm-smi") is not None


def accel_suffixes() -> list[str]:
    """Accel-variant suffixes for this device, most-preferred first, the cpu
    baseline ("") always last. Mirrors the plugin prebuilt selection: Windows
    prefers a CUDA build then Vulkan; Linux prefers Vulkan when a GPU is present;
    macOS ships Metal in the base build (no suffix). The manifest decides which of
    these actually exist for a given engine; the client just orders preference."""
    if sys.platform == "darwin":
        return [""]
    is_arm = platform.machine().lower() in ("arm64", "aarch64")
    sfx: list[str] = []
    if sys.platform in ("win32", "cygwin"):
        cu = _cuda_major()
        if cu:
            sfx.append("-cuda-13.3" if cu >= 13 else "-cuda-12.4")
        if _has_gpu():
            sfx.append("-vulkan")
    elif sys.platform.startswith("linux") and not is_arm and _has_gpu():
        sfx.append("-vulkan")
    sfx.append("")  # cpu / portable baseline, always the final fallback
    return sfx


def variant_candidates() -> list[str]:
    """Ordered ``<platform-arch>[-<accel>]`` tokens to try against the manifest,
    best-accel first and the cpu baseline last."""
    base = platform_arch()
    return [f"{base}{s}" for s in accel_suffixes()]


@dataclass(frozen=True)
class Variant:
    """One resolved artifact: where to GET it, and what it must hash to."""

    path: str  # relative to the store base
    sha256: str
    size: int | None = None

    def url(self, base: str | None = None) -> str:
        return f"{base or base_url()}/{self.path.lstrip('/')}"


class Manifest:
    """Parsed ``index/manifest.v1.json``. Nesting mirrors the store layout:
    capability -> name -> version -> platform-arch -> variant."""

    def __init__(self, data: dict[str, Any]) -> None:
        schema = data.get("schema")
        if schema != 1:
            raise ManifestError(f"unsupported manifest schema: {schema!r} (want 1)")
        self._data = data

    def default_version(self, capability: str, name: str) -> str:
        """The store's current default version for an engine, so the device need
        not pin versions itself. Set by the publish job."""
        try:
            return self._data["defaults"][capability][name]
        except (KeyError, TypeError):
            raise VariantNotFound(f"no default version for {capability}/{name} in manifest") from None

    def variant(self, capability: str, name: str, version: str | None = None, arch: str | None = None) -> Variant:
        """Resolve the artifact for this device. With ``arch`` given, that exact
        variant is required; otherwise the device's ordered ``variant_candidates``
        (best accel first, cpu baseline last) are tried and the first present wins,
        so a GPU box gets the GPU build when published and degrades to cpu when not."""
        version = version or self.default_version(capability, name)
        candidates = [arch] if arch else variant_candidates()
        for cand in candidates:
            try:
                rec = self._data[capability][name][version][cand]
            except (KeyError, TypeError):
                continue
            path, sha = rec.get("path"), rec.get("sha256")
            if not path or not sha:
                raise ManifestError(f"manifest entry {capability}/{name}/{version}/{cand} missing path/sha256")
            return Variant(path=path, sha256=sha, size=rec.get("size"))
        raise VariantNotFound(f"{capability}/{name}/{version}: none of {candidates} in manifest")


def _http_get(url: str, timeout: int, max_bytes: int | None = None) -> bytes:
    """GET ``url`` into memory, retrying transient failures. Cross-host CDNs drop
    connections; 4xx fails fast, 5xx/network retries. Optional size cap."""
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed https/file base
                data = resp.read(max_bytes + 1) if max_bytes else resp.read()
            if max_bytes and len(data) > max_bytes:
                raise ArtifactStoreError(f"response from {url} exceeds {max_bytes} bytes")
            return data
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == attempts:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead):
            if attempt == attempts:
                raise
        time.sleep(2 * attempt)
    raise ArtifactStoreError(f"unreachable retry exit for {url}")  # pragma: no cover


def fetch_manifest(base: str | None = None) -> Manifest:
    """Download + parse the store manifest.

    The URL carries a cache-busting query param: the manifest is the store's
    one mutable object, and an edge-cached stale copy would pin devices to
    superseded artifacts (whose hashes then no longer match the store).
    """
    url = f"{base or base_url()}/{MANIFEST_PATH}?cb={int(time.time())}"
    try:
        raw = _http_get(url, _MANIFEST_TIMEOUT, max_bytes=_MAX_MANIFEST_BYTES)
        return Manifest(json.loads(raw))
    except ArtifactStoreError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ManifestError(f"could not fetch manifest from {url}: {e}") from e


def _download_to(url: str, dest: Path) -> None:
    """Stream ``url`` to ``dest`` via a .partial sidecar, retrying transient errors."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".partial")
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as resp, open(partial, "wb") as fh:  # noqa: S310
                shutil.copyfileobj(resp, fh)
            os.replace(partial, dest)
            return
        except urllib.error.HTTPError as e:
            partial.unlink(missing_ok=True)
            if e.code < 500 or attempt == attempts:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead):
            partial.unlink(missing_ok=True)
            if attempt == attempts:
                raise
        time.sleep(2 * attempt)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract(archive: Path, dest_dir: Path) -> None:
    """Extract a .tar.gz or .zip flat into ``dest_dir``. Engine archives carry the
    server binary plus the shared libraries it links, so every regular file lands
    beside the binary; symlinks (versioned .so/.dylib names) are recreated."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as tf:
            for m in tf.getmembers():
                if m.isfile():
                    src = tf.extractfile(m)
                    if src is not None:
                        out = dest_dir / Path(m.name).name
                        # Stream, don't read() the whole member: engine .so's are
                        # large and edge devices are memory-constrained.
                        with src, open(out, "wb") as fh:
                            shutil.copyfileobj(src, fh)
                        out.chmod(0o755)
            for m in tf.getmembers():
                if m.issym():
                    link = dest_dir / Path(m.name).name
                    if not link.exists():
                        try:
                            link.symlink_to(Path(m.linkname).name)
                        except OSError as exc:
                            # Extraction stays usable without the alias (the real
                            # file is already in place); typical on filesystems
                            # without symlink support.
                            logger.debug(f"skipping symlink {link.name} -> {m.linkname}: {exc}")
    elif name.endswith(".zip"):
        with zipfile.ZipFile(archive, "r") as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                out = dest_dir / Path(member).name
                with zf.open(member) as src, open(out, "wb") as fh:
                    shutil.copyfileobj(src, fh)
                if not sys.platform.startswith("win"):
                    out.chmod(0o755)
    else:
        raise ArtifactStoreError(f"unknown archive type: {archive.name}")


def fetch_variant(variant: Variant, cache_dir: Path, *, base: str | None = None) -> Path:
    """Download ``variant`` to ``cache_dir``, verify its sha256 against the
    manifest, and return the archive path. Raises VerificationError on mismatch
    (the file is discarded). Idempotent: a cached archive that already matches the
    hash is not re-downloaded."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / Path(variant.path).name
    if archive.exists() and _sha256_file(archive) == variant.sha256:
        logger.info(f"artifact {archive.name} already present and verified")
        return archive
    url = variant.url(base)
    logger.info(f"fetching artifact {url}")
    _download_to(url, archive)
    # Cheap size gate before the (more expensive) hash: a wrong-sized file can
    # never verify, and the mismatch message is more diagnosable than a hash diff.
    if variant.size is not None and archive.stat().st_size != variant.size:
        actual_size = archive.stat().st_size
        archive.unlink(missing_ok=True)
        raise VerificationError(f"size mismatch for {variant.path}: got {actual_size}, want {variant.size}")
    actual = _sha256_file(archive)
    if actual != variant.sha256:
        archive.unlink(missing_ok=True)
        raise VerificationError(f"sha256 mismatch for {variant.path}: got {actual}, want {variant.sha256}")
    return archive


_LOCK_TIMEOUT_SECONDS = 120.0


@contextlib.contextmanager
def _engine_lock(lock_path: Path) -> Iterator[None]:
    """Cross-process exclusive lock so two first-use fetches of the same engine
    don't clobber each other: flock on POSIX, msvcrt byte locking on Windows
    (polled with a timeout, since it has no blocking mode). Downgrades to
    unlocked only where neither works (unusual fs); the marker recheck under
    the lock still prevents double work."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
                while True:
                    try:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            logger.warning(f"engine lock timeout on {lock_path.name}; continuing unlocked")
                            break
                        time.sleep(0.25)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                locked = True
        except (ImportError, OSError) as exc:
            logger.debug(f"engine lock unavailable ({exc}); continuing unlocked")
        try:
            yield
        finally:
            if locked and os.name == "nt":
                import msvcrt

                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError as exc:
                    # Non-fatal: the byte lock dies with the handle anyway.
                    logger.debug(f"engine unlock failed ({exc})", exc_info=True)
            # POSIX flock releases on close.


def _atomic_swap(staging: Path, dest: Path) -> None:
    """Replace ``dest`` with ``staging`` via atomic renames, so a reader never sees
    a half-extracted or mixed-version engine dir."""
    old = dest.parent / f".{dest.name}.old"
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        os.replace(dest, old)
    os.replace(staging, dest)
    shutil.rmtree(old, ignore_errors=True)


def ensure_engine(
    name: str,
    version: str | None = None,
    dest_dir: Path | None = None,
    *,
    manifest: Manifest | None = None,
    base: str | None = None,
    arch: str | None = None,
) -> Path:
    """Ensure engine ``name`` is present + verified under ``dest_dir``, fetching it
    from the store on demand. ``version`` defaults to the manifest's per-engine
    default, so a headless device need not pin versions. Returns ``dest_dir``.
    Idempotent, and a locked + atomic transaction: concurrent first-use calls
    can't corrupt the dir, and a version change swaps in a clean dir (no stale
    files). The lazy first-use entry point for a no-bundled-engines install."""
    if dest_dir is None:
        raise ValueError("dest_dir is required")
    manifest = manifest or fetch_manifest(base)
    # Resolve the default up front so logs report the real version, not None.
    version = version or manifest.default_version(CAPABILITY_ENGINES, name)
    variant = manifest.variant(CAPABILITY_ENGINES, name, version, arch)
    marker = dest_dir / ".artifact-sha256"

    def _installed() -> bool:
        return dest_dir.is_dir() and marker.exists() and marker.read_text(encoding="utf-8").strip() == variant.sha256

    if _installed():
        return dest_dir  # already installed at this exact artifact

    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    with _engine_lock(dest_dir.parent / f".{dest_dir.name}.lock"):
        if _installed():
            return dest_dir  # another process installed it while we waited
        cache_dir = dest_dir.parent / f".{dest_dir.name}-cache"
        archive = fetch_variant(variant, cache_dir, base=base)
        # Extract into a fresh staging dir, then atomically swap it into place.
        staging = dest_dir.parent / f".{dest_dir.name}.staging"
        shutil.rmtree(staging, ignore_errors=True)
        _extract(archive, staging)
        (staging / ".artifact-sha256").write_text(variant.sha256, encoding="utf-8")
        _atomic_swap(staging, dest_dir)
        archive.unlink(missing_ok=True)
    logger.info(f"engine {name}@{version} ready at {dest_dir}")
    return dest_dir
