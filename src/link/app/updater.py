# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Bundled OTA update logic.

The frozen install (PyInstaller artifact under ``<install_root>/``) downloads a
release tarball, verifies, extracts, flips the ``current`` pointer, health-checks
the new runtime, and GCs the old. Entry points in the ``Bundle OTA`` section:
``discover_install_root``, ``read_manifest``, ``latest_release_for``,
``download``, ``verify``, ``extract``, ``flip_current``, ``health_check``,
``gc_old_versions``.

The runtime signals an update by setting ``AgentRuntime.update_requested = True``
and shutting down; ``main.run`` then calls ``_apply_update_and_reexec`` which runs
``swap_bundle``. Source installs are developer-only and update via ``git pull``.

Bundled-install layout this module operates on::

    <install_root>/
    ├── locai-link               (supervisor binary; not our concern here)
    ├── versions/<v>/locai-link-runtime
    ├── current -> versions/<v>  (symlink OR a ``CURRENT`` text file)
    ├── previous -> versions/<v> (same shape as current)
    ├── staging/                 (downloads in flight)
    └── data/                    (configs, sessions, models — never touched here)

The launcher is the stable entry point the OS service starts; it follows the
``current`` pointer and exec's the runtime there. That indirection is what
makes A/B updates safe: we extract the new version next to the old, flip
the pointer atomically, and the next launch picks it up.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

import requests
from packaging.version import Version

from link import constants
from link.utils import archive as archive_util

logger = logging.getLogger(__name__)


class _HttpGetter(Protocol):
    """Minimal .get() surface: a real ``requests.Session``, the ``requests``
    module itself, or a test stub, all called with the same keyword args."""

    def get(self, url: str, **kwargs: Any) -> Any: ...


# ===========================================================================
# Bundle OTA: download / verify / extract / flip / GC for PyInstaller bundles.
# ===========================================================================

# ---------------------------------------------------------------------------
# Layout constants: single source of truth for the on-disk shape
# ---------------------------------------------------------------------------

VERSIONS_DIR = "versions"
STAGING_DIR = "staging"
CURRENT_LINK = "current"
PREVIOUS_LINK = "previous"
# Windows-without-Developer-Mode fallback for the symlink. The build process
# writes one or the other depending on what the OS allows; the Rust supervisor
# (in ``crates/link``) accepts either shape.
CURRENT_POINTER_FILE = "CURRENT"
PREVIOUS_POINTER_FILE = "PREVIOUS"
MANIFEST_NAME = "manifest.json"
RUNTIME_BINARY = "locai-link-runtime.exe" if sys.platform == "win32" else "locai-link-runtime"
UPDATE_PENDING_STAMP = ".update-pending"

DEFAULT_RELEASES_REPO = constants.REPO_SLUG

# Download tuning. Generous: this runs once per OTA, not in a hot path.
_CHUNK_SIZE = 1024 * 1024  # 1 MiB
_DOWNLOAD_TIMEOUT = 60  # seconds for connect/read on a single chunk
_GH_API_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BundleUpdateError(Exception):
    """Anything that goes wrong in the bundle OTA path raises a subclass of this."""


class InstallRootNotFound(BundleUpdateError):
    """Could not locate ``manifest.json`` walking up from ``sys.executable``."""


class ManifestMalformed(BundleUpdateError):
    """``manifest.json`` is missing required fields or unparseable."""


class ReleaseNotFound(BundleUpdateError):
    """GitHub API returned a release but no asset matched our stem + version."""


class DownloadFailed(BundleUpdateError):
    """Network or HTTP error during download, after retries."""


class VerifyFailed(BundleUpdateError):
    """SHA mismatch or platform-signature verification failed."""


class ExtractRefused(BundleUpdateError):
    """Tarball/zip contained an entry that would escape the destination directory."""


class HealthCheckFailed(BundleUpdateError):
    """``--self-check`` of the newly extracted bundle did not exit 0 in time."""


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Manifest:
    """Mirror of ``manifest.json`` written by ``bundling/manifest.py::write_manifest``."""

    manifest_version: int
    asset_name: str  # e.g. "locai-link-llm-linux-x86_64"
    version: str  # e.g. "1.0.15"
    git_sha: str
    built_at: str
    plugins: list[dict[str, Any]]
    # UI-app content hashes ({"companion": "<sha256>", ...}), injected at package
    # time. Drives whole-app OTA: an app is re-swapped only when its hash differs
    # from the installed one. Empty on bundles built before whole-app OTA.
    apps: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class ReleaseInfo:
    """Resolved target of an update: where to download from and what to expect."""

    version: str  # the tag minus the "v" prefix
    tag: str  # the raw tag, e.g. "v1.0.16"
    asset_name: str  # full filename including the version suffix and extension
    download_url: str
    sha256_url: str | None  # sibling .sha256; None if release didn't ship one
    checksums_url: str | None = None  # release-wide checksums.txt; preferred over the sidecar


ProgressFn = Callable[[int, int], None]
"""``progress(bytes_done, bytes_total)``; ``bytes_total`` is 0 when unknown."""


# ---------------------------------------------------------------------------
# Layout discovery
# ---------------------------------------------------------------------------


def discover_install_root(start: Path | None = None) -> Path:
    """Walk up from ``sys.executable`` (or ``start``) to find the install_root.

    Defined as the nearest ancestor that contains either ``current``, the
    ``CURRENT`` pointer file, or a ``versions/`` directory. That heuristic
    avoids depending on the manifest, which lives one layer deeper.
    """
    if start is None:
        start = Path(sys.executable).resolve()
    here = start if start.is_dir() else start.parent
    for candidate in (here, *here.parents):
        if (candidate / CURRENT_LINK).exists() or (candidate / CURRENT_POINTER_FILE).is_file():
            return candidate
        if (candidate / VERSIONS_DIR).is_dir():
            return candidate
    raise InstallRootNotFound(f"No install_root found walking up from {start}")


def read_manifest(root: Path) -> Manifest:
    """Parse ``<root>/<current>/manifest.json`` into a Manifest."""
    current = _resolve_current(root)
    if current is None:
        raise InstallRootNotFound(f"No current version under {root}")
    path = current / MANIFEST_NAME
    if not path.is_file():
        raise ManifestMalformed(f"manifest.json missing at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestMalformed(f"{path}: {exc}") from exc
    try:
        return Manifest(
            manifest_version=int(data["manifest_version"]),
            asset_name=str(data["asset_name"]),
            version=str(data["version"]),
            git_sha=str(data.get("git_sha", "")),
            built_at=str(data.get("built_at", "")),
            plugins=list(data.get("plugins", [])),
            apps={str(k): str(v) for k, v in (data.get("apps") or {}).items()},
        )
    except KeyError as exc:
        raise ManifestMalformed(f"{path}: missing field {exc}") from exc


# ---------------------------------------------------------------------------
# Release resolution
# ---------------------------------------------------------------------------


def _platform_tag() -> str:
    """The ``<os>-<arch>`` segment the release workflow inserts into asset
    names (e.g. ``linux-x86_64``). Must match the ``platform_tag`` matrix in
    .github/workflows/release.yml."""
    os_tag = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    arch_tag = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
    return f"{os_tag}-{arch_tag}"


def _ota_overrides_allowed() -> bool:
    """Honour the LOCAI_* release overrides only outside a frozen production
    bundle, or with an explicit opt-in. Stops a hostile or misconfigured env
    from redirecting a production device's OTA to an attacker-controlled host."""
    return not running_frozen_bundle() or bool(os.environ.get("LOCAI_ALLOW_OTA_OVERRIDES"))


def latest_release_for(
    asset_stem: str,
    *,
    repo: str | None = None,
    api_base: str | None = None,
    session: _HttpGetter | None = None,
    platform_tag: str | None = None,
) -> ReleaseInfo:
    """Find the latest release that publishes an asset matching ``asset_stem``.

    ``asset_stem`` is the plugin-set base from ``manifest.asset_name`` (e.g.
    ``locai-link-llm-stt``). The release workflow appends this host's
    platform/arch and the version: ``<stem>-<platform_tag>-v<version>.<ext>``.
    ``platform_tag`` defaults to this host's tag so an install only ever matches
    its own platform's asset.

    ``repo`` / ``api_base`` fall back to ``LOCAI_RELEASES_REPO`` /
    ``LOCAI_RELEASES_API_BASE`` for local testing (see bundling/serve_local_release.py),
    but a frozen production bundle ignores those unless ``LOCAI_ALLOW_OTA_OVERRIDES``
    is set, so the env can't redirect a real device's OTA.
    """
    allow_env = _ota_overrides_allowed()
    repo = repo or (os.environ.get("LOCAI_RELEASES_REPO") if allow_env else None) or DEFAULT_RELEASES_REPO
    api_base = (
        api_base or (os.environ.get("LOCAI_RELEASES_API_BASE") if allow_env else None) or "https://api.github.com"
    )
    if not allow_env and not api_base.startswith("https://"):
        raise ReleaseNotFound(f"refuse insecure release API base: {api_base!r}")
    ptag = platform_tag or _platform_tag()
    http = session or requests
    url = f"{api_base.rstrip('/')}/repos/{repo}/releases/latest"
    try:
        resp = http.get(url, timeout=_GH_API_TIMEOUT, headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise ReleaseNotFound(f"GitHub releases lookup failed: {exc}") from exc
    payload = resp.json()
    tag = str(payload.get("tag_name") or "")
    if not tag:
        raise ReleaseNotFound(f"Release at {url} has no tag_name")
    version = tag.lstrip("v")

    assets: Iterable[dict[str, Any]] = payload.get("assets") or []
    asset_match, sha_match = _pick_assets(assets, asset_stem, version, ptag)
    if asset_match is None:
        raise ReleaseNotFound(f"No asset matching '{asset_stem}-{ptag}-v{version}.(tar.gz|zip)' on release {tag}")
    checksums = next((a for a in assets if (a.get("name") or "").lower() == "checksums.txt"), None)
    return ReleaseInfo(
        version=version,
        tag=tag,
        asset_name=str(asset_match["name"]),
        download_url=str(asset_match["browser_download_url"]),
        sha256_url=str(sha_match["browser_download_url"]) if sha_match else None,
        checksums_url=str(checksums["browser_download_url"]) if checksums else None,
    )


def _pick_assets(
    assets: Iterable[dict[str, Any]], stem: str, version: str, platform_tag: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    bundle_re = re.compile(rf"^{re.escape(stem)}-{re.escape(platform_tag)}-v{re.escape(version)}\.(tar\.gz|zip)$")
    sha_re = re.compile(rf"^{re.escape(stem)}-{re.escape(platform_tag)}-v{re.escape(version)}\.(tar\.gz|zip)\.sha256$")
    bundle_match: dict[str, Any] | None = None
    sha_match: dict[str, Any] | None = None
    for a in assets:
        name = a.get("name") or ""
        if bundle_re.match(name) and bundle_match is None:
            bundle_match = a
        elif sha_re.match(name) and sha_match is None:
            sha_match = a
    return bundle_match, sha_match


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def download(
    url: str,
    dest: Path,
    *,
    progress: ProgressFn | None = None,
    session: _HttpGetter | None = None,
    max_retries: int = 3,
) -> Path:
    """Stream ``url`` into ``dest`` via a ``.partial`` sidecar; rename on completion.

    Resumable: if ``dest.partial`` exists from a previous run it's reused with a
    Range request. Returns the path to the completed file (== ``dest``).
    """
    http = session or requests
    partial = dest.with_suffix(dest.suffix + ".partial")
    dest.parent.mkdir(parents=True, exist_ok=True)

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        existing = partial.stat().st_size if partial.is_file() else 0
        headers = {"Range": f"bytes={existing}-"} if existing else {}
        try:
            with http.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT, headers=headers) as r:
                # Partial Content is fine; 200 with no Range header is also fine.
                if r.status_code not in (200, 206):
                    raise DownloadFailed(f"HTTP {r.status_code} fetching {url}")
                total = existing + int(r.headers.get("Content-Length") or 0)
                mode = "ab" if r.status_code == 206 and existing else "wb"
                if mode == "wb":
                    existing = 0
                done = existing
                with partial.open(mode) as fh:
                    if progress is not None:
                        progress(done, total)
                    for chunk in r.iter_content(chunk_size=_CHUNK_SIZE):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(done, total)
            os.replace(partial, dest)
            return dest
        except (requests.RequestException, DownloadFailed) as exc:
            last_exc = exc
            logger.warning(f"download attempt {attempt}/{max_retries} for {url} failed: {exc}")
    raise DownloadFailed(f"Giving up after {max_retries} attempts on {url}: {last_exc}")


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def verify(
    path: Path,
    *,
    expected_sha256: str | None = None,
    expected_sha256_url: str | None = None,
    platform: str | None = None,
    session: _HttpGetter | None = None,
) -> None:
    """Verify a downloaded bundle. Raises ``VerifyFailed`` on mismatch.

    SHA check is mandatory: pass either the literal hex digest or a URL to a
    sibling ``.sha256`` file (the format GitHub Actions publishes). Platform
    signature is best-effort and dispatches by OS:
        - macOS: ``codesign --verify --deep --strict`` over the extracted
          bundle (skipped here; runs post-extract via ``verify_extracted_macos``).
        - Linux: SHA is the only gate, by design.
        - Windows: not supported in v1 (no code-signing cert yet).
    """
    if expected_sha256 is None and expected_sha256_url is None:
        raise VerifyFailed("verify() requires expected_sha256 or expected_sha256_url")

    if expected_sha256 is None:
        expected_sha256 = _fetch_sha256(expected_sha256_url or "", session=session)

    actual = _hash_sha256(path)
    if actual.lower() != expected_sha256.lower():
        raise VerifyFailed(f"SHA256 mismatch: expected {expected_sha256}, got {actual}")

    # Platform-signature dispatch is intentionally a no-op for the tarball
    # itself; the macOS codesign check runs against the extracted bundle (see
    # verify_extracted_macos). Keeps the function callable on all platforms.
    if platform == "win32":
        # Documented for v1: SHA is the only integrity gate; Authenticode
        # check requires a signed binary, which we don't have a cert for yet.
        logger.debug("verify: Windows Authenticode check skipped (v1 limitation)")


def verify_extracted_macos(extracted_dir: Path) -> None:
    """Run ``codesign --verify --deep --strict`` over an extracted macOS bundle.

    Separate from ``verify()`` because codesign operates on the laid-out
    binary, not the tarball. Raises ``VerifyFailed`` on rejection.
    """
    if sys.platform != "darwin":
        return
    runtime = extracted_dir / RUNTIME_BINARY
    if not runtime.exists():
        # Nothing to verify; let extraction errors surface elsewhere.
        return
    try:
        subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(runtime)],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise VerifyFailed("codesign not found — required on macOS") from exc
    except subprocess.CalledProcessError as exc:
        raise VerifyFailed(f"codesign rejected {runtime}: {exc.stderr.decode(errors='replace')[:400]}") from exc


def _fetch_sha256(url: str, *, session: _HttpGetter | None = None) -> str:
    http = session or requests
    try:
        resp = http.get(url, timeout=_GH_API_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise VerifyFailed(f"Could not fetch SHA from {url}: {exc}") from exc
    # Format: either bare hex digest, or "<hex>  <filename>" (sha256sum format).
    body = resp.text.strip()
    if not body:
        raise VerifyFailed(f"SHA file at {url} is empty")
    tokens = body.split()
    if not tokens:
        raise VerifyFailed(f"SHA file at {url} is whitespace-only")
    first_token = tokens[0]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", first_token):
        raise VerifyFailed(f"SHA file at {url} did not parse as a hex digest")
    return first_token


def _sha256_from_checksums(url: str, asset_name: str, *, session: _HttpGetter | None = None) -> str:
    """Digest for ``asset_name`` from a release-wide ``checksums.txt``
    (sha256sum format: one ``<hex>  <filename>`` line per asset)."""
    http = session or requests
    try:
        resp = http.get(url, timeout=_GH_API_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise VerifyFailed(f"Could not fetch checksums from {url}: {exc}") from exc
    for line in resp.text.splitlines():
        tokens = line.split()
        if len(tokens) < 2:
            continue
        name = tokens[-1].lstrip("*").removeprefix("./")
        if name == asset_name and re.fullmatch(r"[0-9a-fA-F]{64}", tokens[0]):
            return tokens[0]
    raise VerifyFailed(f"No valid sha256 entry for {asset_name} in {url}")


def _hash_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------


def extract(archive: Path, dest: Path) -> None:
    """Extract a release archive's version payload into ``dest``.

    The release tarball is a full installer package: it wraps the bundle as
    ``<name>/bundle/versions/<v>/`` alongside install.sh + icons at the root.
    For OTA we only want that inner payload dir (the one holding the runtime);
    the launcher, pointer, and installer scripts already exist in the deployed
    install_root and must not be pulled in. ``_locate_versioned_payload`` finds
    it regardless of wrapping depth.

    Refuses path-traversal entries. Atomic replace of ``dest`` on success.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.with_name(dest.name + ".tmp")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    try:
        _extract_archive(archive, staging)
        payload = _locate_versioned_payload(staging)
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(payload, dest)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _extract_archive(archive: Path, staging: Path) -> None:
    """Extract ``archive`` (tar.gz/tgz or zip) into ``staging`` via the shared safe
    extractor, surfacing unsafe/unknown entries as ExtractRefused (the OTA error
    type). Caller owns ``staging`` and its cleanup."""
    try:
        archive_util.extract_archive(archive, staging)
    except (archive_util.UnsafeArchiveEntry, archive_util.UnknownArchiveType) as e:
        raise ExtractRefused(str(e)) from e


def _locate_versioned_payload(staging: Path) -> Path:
    """Find the bundle payload dir (the one holding the runtime binary) inside
    an extracted release, regardless of how the packer wraps it.

    Release tarballs wrap the payload as ``<name>/bundle/versions/<v>/`` (full
    installer package with install.sh + icons at the root). Locating the dir
    that directly contains the runtime binary is layout-agnostic; the
    ``versions/<v>/`` and single-top-level-dir branches remain as fallbacks
    for flat archives without the canonically-named binary.
    """
    runtimes = [p.parent for p in staging.rglob(RUNTIME_BINARY) if p.is_file()]
    if len(runtimes) == 1:
        return runtimes[0]
    if len(runtimes) > 1:
        raise BundleUpdateError(f"multiple runtime binaries in archive: {sorted(str(r) for r in runtimes)}")
    versions_dir = staging / VERSIONS_DIR
    if versions_dir.is_dir():
        children = [p for p in versions_dir.iterdir() if p.is_dir()]
        if len(children) == 1:
            return children[0]
        raise BundleUpdateError(
            f"expected exactly one versions/<v>/ inside archive, found {[c.name for c in children]}"
        )
    # Legacy shape: a single top-level dir wrapping the bundle directly.
    top_dirs = [p for p in staging.iterdir() if p.is_dir()]
    if len(top_dirs) == 1:
        return top_dirs[0]
    raise BundleUpdateError("archive layout unrecognised: no runtime binary or recognizable payload dir found")


# ---------------------------------------------------------------------------
# Current/previous pointer management
# ---------------------------------------------------------------------------


def _resolve_current(root: Path) -> Path | None:
    """Return the absolute path of ``versions/<v>/`` for the live version."""
    return _resolve_pointer(root, CURRENT_LINK, CURRENT_POINTER_FILE)


def _resolve_pointer(root: Path, link_name: str, file_name: str) -> Path | None:
    link = root / link_name
    if link.is_symlink() or link.is_dir():
        try:
            resolved = link.resolve()
            if resolved.is_dir():
                return resolved
        except OSError:
            pass
    pointer = root / file_name
    if pointer.is_file():
        version = pointer.read_text(encoding="utf-8").strip()
        target = root / VERSIONS_DIR / version
        if target.is_dir():
            return target
    return None


def flip_current(root: Path, new_version: str) -> None:
    """Point ``current`` at ``versions/<new_version>``, demote the old to ``previous``.

    Preserves the pointer *shape*: if the install was using a CURRENT pointer
    file (Windows-without-developer-mode fallback chosen at build time), the
    new pointer is written the same way. Otherwise a symlink is used.
    """
    new_target = root / VERSIONS_DIR / new_version
    if not new_target.is_dir():
        raise BundleUpdateError(f"Cannot flip current to missing version: {new_target}")

    use_symlink = _install_uses_symlink(root)
    old_current_version = _read_pointer_version(root, CURRENT_LINK, CURRENT_POINTER_FILE)

    _write_pointer(root, CURRENT_LINK, CURRENT_POINTER_FILE, new_version, use_symlink=use_symlink)

    # `previous` only makes sense if we actually had a current to demote.
    if old_current_version and old_current_version != new_version:
        _write_pointer(
            root,
            PREVIOUS_LINK,
            PREVIOUS_POINTER_FILE,
            old_current_version,
            use_symlink=use_symlink,
        )


def _install_uses_symlink(root: Path) -> bool:
    """The build process chose between symlink and pointer-file. Match what's there."""
    if (root / CURRENT_LINK).is_symlink():
        return True
    if (root / CURRENT_POINTER_FILE).is_file():
        return False
    # Fresh install with no current yet: prefer symlink; the OS will tell us
    # if it isn't allowed and `_write_pointer` falls back.
    return sys.platform != "win32"


def _read_pointer_version(root: Path, link_name: str, file_name: str) -> str | None:
    link = root / link_name
    if link.is_symlink():
        return Path(os.readlink(link)).name
    pointer = root / file_name
    if pointer.is_file():
        return pointer.read_text(encoding="utf-8").strip() or None
    return None


def _write_pointer(
    root: Path,
    link_name: str,
    file_name: str,
    version: str,
    *,
    use_symlink: bool,
) -> None:
    """Atomically replace the pointer for ``link_name`` / ``file_name``.

    Symlink path: create a uniquely-named temp symlink in the same dir, then
    ``os.replace`` it onto the target. POSIX atomic; ``MoveFileEx`` on Windows.
    Pointer-file path: write ``file_name + .tmp``, then ``os.replace``.
    """
    if use_symlink:
        target = root / link_name
        with tempfile.TemporaryDirectory(dir=root) as td:
            tmp = Path(td) / "link.tmp"
            tmp.symlink_to(Path(VERSIONS_DIR) / version, target_is_directory=True)
            # Move into the parent dir under a unique name first: symlinks
            # cannot be atomically replaced across directories on every fs,
            # but os.replace within the same dir is atomic on POSIX.
            staged = root / f".{link_name}.tmp"
            if staged.exists() or staged.is_symlink():
                staged.unlink()
            tmp.rename(staged)
            os.replace(staged, target)
        # Clean up stray pointer file from a previous shape.
        stale = root / file_name
        if stale.is_file():
            stale.unlink()
        return

    # Pointer-file shape.
    pointer = root / file_name
    tmp = root / (file_name + ".tmp")
    tmp.write_text(version + "\n", encoding="utf-8")
    os.replace(tmp, pointer)
    # Clean up stray symlink from a previous shape.
    link = root / link_name
    if link.is_symlink():
        link.unlink()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def health_check(runtime_path: Path, *, timeout: float = 30.0) -> bool:
    """Spawn ``runtime_path --self-check`` and return True on a clean exit 0.

    Captures stderr for log forwarding on failure. Does not raise; the caller
    decides what to do on a False result (typically: skip the flip, GC the
    staged version).
    """
    if not runtime_path.is_file():
        logger.error(f"health_check: runtime not found at {runtime_path}")
        return False
    try:
        result = subprocess.run(
            [str(runtime_path), "self-check"],
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        logger.error(f"health_check: timed out after {timeout}s on {runtime_path}")
        return False
    except OSError as exc:
        logger.error(f"health_check: could not exec {runtime_path}: {exc}")
        return False
    if result.returncode != 0:
        logger.error(f"health_check: exit {result.returncode} on {runtime_path}\nstderr: {result.stderr[:800]}")
        return False
    return True


# ---------------------------------------------------------------------------
# GC
# ---------------------------------------------------------------------------


def gc_old_versions(root: Path, *, keep: int = 2) -> list[str]:
    """Delete versions that are neither current nor previous (plus N-2 oldest).

    ``keep`` is the upper bound *including* current and previous. Default 2 =
    current + previous, nothing else retained. Returns the list of removed
    version names for logging.
    """
    versions_dir = root / VERSIONS_DIR
    if not versions_dir.is_dir():
        return []

    current_version = _read_pointer_version(root, CURRENT_LINK, CURRENT_POINTER_FILE)
    previous_version = _read_pointer_version(root, PREVIOUS_LINK, PREVIOUS_POINTER_FILE)
    pinned = {v for v in (current_version, previous_version) if v}

    # Sort newest-first by mtime so a `keep > 2` retains the freshest.
    candidates = sorted(
        (p for p in versions_dir.iterdir() if p.is_dir() and not p.name.endswith(".tmp")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    keepers: set[str] = set(pinned)
    for entry in candidates:
        if len(keepers) >= keep:
            break
        keepers.add(entry.name)

    removed: list[str] = []
    for entry in candidates:
        if entry.name in keepers:
            continue
        try:
            shutil.rmtree(entry)
            removed.append(entry.name)
        except OSError as exc:
            logger.warning(f"gc_old_versions: could not remove {entry}: {exc}")
    return removed


# ---------------------------------------------------------------------------
# Staging helpers
# ---------------------------------------------------------------------------


def staging_path(root: Path) -> Path:
    p = root / STAGING_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def clear_staging(root: Path) -> None:
    p = root / STAGING_DIR
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def running_frozen_bundle() -> bool:
    """True when this process is a PyInstaller-frozen bundle (vs a source install)."""
    return bool(getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None))


def _version_gt(a: str, b: str) -> bool:
    """True if version ``a`` is newer than ``b`` (PEP 440 comparison)."""
    return Version(a) > Version(b)


# Control's public version-check endpoint; appended to the device's api_url.
LATEST_VERSION_PATH = "/devices/agent/latest-version"
DEFAULT_CONTROL_API_BASE = constants.DEFAULT_API_URL


def latest_version_from_control(base_url: str, *, session: _HttpGetter | None = None) -> str:
    """Latest published agent version from Control's cached endpoint (rate-limit
    safe for fleets, vs. polling GitHub per device). Returns e.g. ``1.1.0``."""
    http = session or requests
    url = f"{base_url.rstrip('/')}{LATEST_VERSION_PATH}"
    resp = http.get(url, timeout=_GH_API_TIMEOUT, headers={"Accept": "application/json"})
    resp.raise_for_status()
    version = str((resp.json() or {}).get("latest_version") or "")
    if not version:
        raise ReleaseNotFound(f"latest-version response missing 'latest_version': {url}")
    return version


def check_update_available(
    install_root: Path | None = None, control_base_url: str | None = None
) -> tuple[bool, str | None]:
    """Whether a newer bundle is published (frozen installs only).

    Version check hits Control's endpoint; the OTA download still resolves the
    per-platform asset from GitHub in ``swap_bundle``. Best-effort: any failure
    (offline, source install, Control error) yields ``(False, None)``.

    ``LOCAI_LATEST_VERSION`` forces the "latest" version for local testing (frozen
    bundles honour it only with ``LOCAI_ALLOW_OTA_OVERRIDES`` set).
    """
    if not running_frozen_bundle():
        return (False, None)
    try:
        root = install_root or discover_install_root()
        manifest = read_manifest(root)
        override = os.environ.get("LOCAI_LATEST_VERSION") if _ota_overrides_allowed() else None
        latest = override or latest_version_from_control(control_base_url or DEFAULT_CONTROL_API_BASE)
        return (_version_gt(latest, manifest.version), latest)
    except Exception as e:  # noqa: BLE001 - never let the check crash the agent
        logger.debug(f"update check failed: {e}")
        return (False, None)


def bundle_asset_available(install_root: Path | None = None) -> bool:
    """Whether the latest release actually publishes an installable per-platform
    asset for this bundle.

    Pre-flight for the OTA path: if no asset exists (e.g. the macOS tarball isn't
    published), accepting the update just shuts the agent down, fails in
    ``swap_bundle`` with ``ReleaseNotFound``, relaunches, and retries forever.
    The caller uses this to decline the update and stay on the current version
    instead. Frozen installs only; source installs update via git, so this
    returns ``False`` and the caller must gate on ``running_frozen_bundle``.
    """
    if not running_frozen_bundle():
        return False
    try:
        root = install_root or discover_install_root()
        manifest = read_manifest(root)
        latest_release_for(manifest.asset_name)
        return True
    except Exception as e:  # noqa: BLE001 - never let the pre-flight crash the agent
        logger.debug(f"bundle_asset_available: no installable asset: {e}")
        return False


# ---------------------------------------------------------------------------
# Whole-app OTA: swap the changed UI app (the single desktop app)
# ---------------------------------------------------------------------------

_APP_COMPANION = "companion"


def _ui_app_payload_name(key: str) -> str:
    """Name of the app inside the OTA payload for the current platform."""
    if sys.platform == "darwin":
        return {"companion": "Locai Link.app"}[key]
    if sys.platform == "win32":
        return {"companion": "companion.exe"}[key]
    return {"companion": "companion"}[key]


def _ui_app_destinations(key: str, install_root: Path) -> list[Path]:
    """Installed app copy the OTA can update. On macOS this is the install-root
    copy (user-owned, so the user-context OTA can replace it); the LaunchAgent
    runs that copy. The /Applications copy is pkg-managed for discoverability and
    left alone (writing there needs admin), refreshed on the next pkg install."""
    if sys.platform == "darwin":
        return [install_root / "Locai Link.app"]
    if sys.platform == "win32":
        return []  # no companion OTA on Windows yet
    return [install_root / "companion"]


def _rm(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        path.unlink(missing_ok=True)


def _remove_legacy_setup_assistant(install_root: Path) -> None:
    """Remove the pre-merge standalone Setup Assistant left on disk by an
    upgrade-in-place OTA (onboarding is now a window of the main app). The pkg
    postinstall + uninstaller cover the reinstall/uninstall paths; this covers
    OTA-only devices. Best-effort: a leftover is harmless (no LaunchAgent
    targets it). LEGACY-SA-CLEANUP: remove once no pre-merge install remains."""
    targets: list[Path] = []
    if sys.platform == "darwin":
        # Only the user-owned install-root copy is removable from the user-context
        # OTA; the /Applications copy is pkg-managed (root-owned) and clears on the
        # next pkg reinstall/uninstall.
        targets.append(install_root / "Setup Assistant.app")
    elif sys.platform.startswith("linux"):
        targets.append(install_root / "setup-assistant")
        targets.append(Path.home() / ".local" / "share" / "applications" / "locai-setup-assistant.desktop")
    for t in targets:
        try:
            if t.exists() or t.is_symlink():
                _rm(t)
                logger.info(f"removed legacy Setup Assistant artifact: {t}")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"legacy Setup Assistant cleanup skipped {t}: {e}")


def _locate_in_payload(staging: Path, name: str) -> Path | None:
    """Shallowest entry named ``name`` in the extracted payload, or None."""
    matches = sorted(staging.rglob(name), key=lambda p: len(p.parts))
    return matches[0] if matches else None


def _install_app(src: Path, dest: Path) -> None:
    """Copy ``src`` (file or dir) onto ``dest`` via a same-dir temp + os.replace.
    Copies (not moves) so one payload can populate multiple destinations."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.new")
    _rm(tmp)
    if src.is_dir():
        if sys.platform == "darwin":
            # ditto preserves the code signature + xattrs that copytree drops,
            # so the swapped .app isn't rejected by Gatekeeper as damaged.
            subprocess.run(["ditto", str(src), str(tmp)], check=True)
        else:
            shutil.copytree(src, tmp, symlinks=True)
    else:
        shutil.copy2(src, tmp)
    # os.replace is atomic for files/empty dirs; .app bundles are non-empty dirs
    # it can't overwrite, so move the old one aside and only drop it once the new
    # one lands (a crash mid-swap leaves a recoverable .old, not a missing app).
    try:
        os.replace(tmp, dest)
    except OSError:
        backup = dest.with_name(f".{dest.name}.old")
        _rm(backup)
        if dest.exists() or dest.is_symlink():
            os.replace(dest, backup)
        os.replace(tmp, dest)
        _rm(backup)


def _macos_console_uid() -> str | None:
    """UID of the logged-in GUI user, whose launchd ``gui`` domain owns the
    companion. The OTA process may run in a different context than that session."""
    try:
        out = subprocess.run(["stat", "-f%u", "/dev/console"], capture_output=True, text=True, timeout=5)
        uid = out.stdout.strip()
        return uid if uid.isdigit() else None
    except Exception:  # noqa: BLE001
        return None


_COMPANION_LABEL = constants.COMPANION_LABEL


def _restart_ui_app(key: str) -> None:
    """Relaunch the app so it picks up the swapped binary. Best-effort."""
    if key != _APP_COMPANION:
        return
    try:
        if sys.platform == "darwin":
            _restart_companion_macos()
        elif sys.platform.startswith("linux"):
            subprocess.run(
                ["systemctl", "--user", "restart", "locai-link-companion.service"],
                check=False,
                timeout=15,
            )
    except Exception as e:  # noqa: BLE001 - a failed restart shouldn't fail the update
        logger.warning(f"Could not restart companion after update: {e}")


def _current_uid() -> str:
    """os.getuid() is POSIX-only; fall back to "0" where it is unavailable (e.g.
    Windows), so these darwin-only helpers stay importable/testable cross-platform."""
    getuid = getattr(os, "getuid", None)
    return str(getuid()) if getuid else "0"


def _home_for_uid(uid: str) -> Path:
    """Home directory of ``uid`` (the console user) - where the companion
    LaunchAgent plist lives. Path.home() would give the updater's own home (e.g.
    /var/root when it runs as root), so resolve the target user's home instead."""
    try:
        import pwd

        return Path(pwd.getpwuid(int(uid)).pw_dir)
    except Exception:  # noqa: BLE001
        return Path.home()


def _restart_companion_macos(force_reload: bool = False) -> None:
    """Relaunch the companion:
    kickstart in place; if the service isn't reachable in this domain (stale /
    legacy-domain registration), rebootstrap from the installed plist and retry;
    fall back to LaunchServices. Each launchctl call is bounded so a hung
    kickstart can't stall the update. Pass force_reload=True when the plist on
    disk just changed (self-heal): skip the in-place kickstart and bootout +
    bootstrap so launchd reloads the corrected definition instead of restarting
    the stale in-memory job."""
    uid = _macos_console_uid() or _current_uid()
    service = f"gui/{uid}/{_COMPANION_LABEL}"
    plist = _home_for_uid(uid) / "Library" / "LaunchAgents" / f"{_COMPANION_LABEL}.plist"

    def _lc(*args: str) -> int:
        try:
            return subprocess.run(["launchctl", *args], check=False, timeout=10).returncode
        except subprocess.TimeoutExpired:
            return 1  # treat a hang as failure and move on; never block the update

    # Non-destructive first: kickstart a live service in place. When the plist
    # just changed, skip this so we reload the corrected definition below.
    ok = False if force_reload else _lc("kickstart", "-k", service) == 0
    if not ok:
        # Not reachable in this domain: refresh the registration and retry.
        _lc("bootout", service)
        _lc("bootstrap", f"gui/{uid}", str(plist))
        # bootstrap with RunAtLoad already starts the (correct) binary; kickstart
        # WITHOUT -k here just ensures it's up, so we don't race a second instance.
        ok = _lc("kickstart", service) == 0
    if not ok:
        # Last resort: LaunchServices. Prefer the OTA-owned install-root copy
        # (the one we de-quarantine), then the pkg-managed /Applications copy.
        for app in (
            Path(constants.MACOS_INSTALL_ROOT) / "Locai Link.app",
            Path("/Applications/Locai Link.app"),
        ):
            if app.exists():
                subprocess.Popen(
                    ["open", "-a", str(app)],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                break


# Latest-release download for a fresh reinstall, per platform (the asset name
# and extension differ by OS/arch). Used when a macOS OTA updated the runtime
# but couldn't swap the UI apps, so the install can't self-heal over OTA.
_REINSTALL_EXT = {"darwin": "pkg", "linux": "tar.gz", "win32": "zip"}


def _reinstall_url() -> str:
    """Permanent 'latest' download URL for this host's platform/arch."""
    ext = _REINSTALL_EXT.get(sys.platform, "pkg")
    return f"https://github.com/{DEFAULT_RELEASES_REPO}/releases/latest/download/locai-link-{_platform_tag()}.{ext}"


def _companion_installed_version(install_root: Path) -> str | None:
    """Version of the running companion .app (macOS), or None if unknown. Checks
    the install-root copy first (the one the LaunchAgent runs and the OTA
    updates) so drift reflects the live UI, not the pkg-managed /Applications copy."""
    if sys.platform != "darwin":
        return None
    import plistlib

    for app in (install_root / "Locai Link.app", Path("/Applications/Locai Link.app")):
        plist = app / "Contents" / "Info.plist"
        if not plist.exists():
            continue
        try:
            data = plistlib.loads(plist.read_bytes())
            version = str(data.get("CFBundleShortVersionString") or "").strip()
            if version:
                return version
        except Exception:  # noqa: BLE001
            continue
    return None


def _companion_running_version(install_root: Path) -> str | None:
    """Version the *running* companion published at launch (state file), or None
    when absent (pre-fix companion). Reflects the live process, unlike the
    on-disk bundle, which reads new right after a swap even if the old companion
    is still running because the relaunch silently failed."""
    marker = install_root / constants.STATE_SUBDIR / constants.COMPANION_RUNNING_VERSION_MARKER
    try:
        version = marker.read_text(encoding="utf-8").strip()
        return version or None
    except OSError:
        return None


def _notify_reinstall_required(version: str, url: str) -> None:
    """Best-effort local notification that a reinstall is needed to finish the
    update. Names the download URL but does not open it; no outbound navigation
    happens without the user choosing to act."""
    msg = f"Couldn't finish updating to {version}. Reinstall from {url} to finish."
    title = "Locai Link update incomplete"
    try:
        # Pass msg/title as osascript args (argv), not interpolated into the
        # AppleScript source, so URL/text content can't alter the program.
        subprocess.run(
            [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 1 of argv) with title (item 2 of argv)",
                "-e",
                "end run",
                msg,
                title,
            ],
            check=False,
            timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"reinstall notification failed: {e}")


# Settle window for the drift check: a just-relaunched companion needs a moment
# to publish its running version. Only spent when a mismatch is seen (the failure
# path), so a healthy update pays nothing.
_DRIFT_SETTLE_TRIES = 3
_DRIFT_SETTLE_SECONDS = 1.5


def _heal_companion_launchagent(install_root: Path) -> bool:
    """Repair a companion LaunchAgent whose ProgramArguments point at a binary that
    never shipped (installs that predate the path fix). The OTA can't refresh the
    plist, so correct it in place on startup, drop any stale open-a companion, and
    re-bootstrap the launchd copy so the tray runs the current build. No-op once the
    plist is already correct. Returns True if it repaired + relaunched."""
    if sys.platform != "darwin":
        return False
    import plistlib

    uid = _macos_console_uid() or _current_uid()
    plist = _home_for_uid(uid) / "Library" / "LaunchAgents" / f"{_COMPANION_LABEL}.plist"
    correct = str(install_root / "Locai Link.app" / "Contents" / "MacOS" / "locai-link")
    try:
        if not plist.exists():
            return False  # fresh installs get the plist from the Setup Assistant
        data = plistlib.loads(plist.read_bytes())
        args = data.get("ProgramArguments") or []
        if args and args[0] == correct:
            return False  # already correct
        data["ProgramArguments"] = [correct]
        tmp = plist.with_name(plist.name + ".new")
        tmp.write_bytes(plistlib.dumps(data))
        os.replace(tmp, plist)
        os.chmod(plist, 0o644)  # macOS 12+ refuses to bootstrap a non-0644 plist
        logger.info(f"self-heal: repaired companion LaunchAgent program path -> {correct}")
        # A companion started via `open -a` isn't launchd-managed, so bootout won't
        # stop it; kill it so the relaunch below doesn't leave two trays.
        subprocess.run(["pkill", "-f", "Locai Link.app/Contents/MacOS/locai-link"], check=False, timeout=10)
        _restart_companion_macos(force_reload=True)
        return True
    except Exception as e:  # noqa: BLE001 - self-heal must never crash the agent
        logger.warning(f"self-heal: could not repair companion LaunchAgent: {e}")
        return False


def check_ui_version_drift(install_root: Path | None = None, url: str | None = None) -> None:
    """Warn once if the macOS companion UI didn't update alongside the runtime.

    Compares the runtime version against the *running* companion version, so it
    catches both a swap that never landed and a swapped bundle whose relaunch
    silently failed (old process still showing the old UI). Prompts a one-time
    reinstall. Frozen macOS installs only; best-effort; at most once per version.
    """
    if not running_frozen_bundle() or sys.platform != "darwin":
        return
    try:
        root = install_root or discover_install_root()
        runtime_version = read_manifest(root).version
        # Proactively repair a stale/broken companion LaunchAgent (installs whose
        # plist predates the path fix) and relaunch the tray. No-op when the plist
        # is already correct; the drift prompt below is the fallback if it can't heal.
        if _heal_companion_launchagent(root):
            time.sleep(_DRIFT_SETTLE_SECONDS)
        running = _companion_running_version(root)
        companion_version = running or _companion_installed_version(root)
        # Only the running-version path has a relaunch race: give a just-
        # relaunched companion a moment to publish its version before deciding
        # it's stale, so we don't fire a false prompt during the relaunch window.
        settle = 0
        while running and companion_version != runtime_version and settle < _DRIFT_SETTLE_TRIES:
            time.sleep(_DRIFT_SETTLE_SECONDS)
            running = _companion_running_version(root)
            companion_version = running or _companion_installed_version(root)
            settle += 1
        if not companion_version or companion_version == runtime_version:
            return
        marker = root / constants.STATE_SUBDIR / "ui-drift-notified"
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == runtime_version:
            return
        logger.warning(
            f"UI apps stale after update: runtime={runtime_version}, companion={companion_version}; reinstall needed"
        )
        _notify_reinstall_required(runtime_version, url or _reinstall_url())
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(runtime_version, encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - the drift check must never crash the agent
        logger.debug(f"ui drift check skipped: {e}")


def _harden_swapped_app(dest: Path) -> None:
    """After a macOS .app swap: strip com.apple.quarantine (which silently blocks
    a launchctl-driven relaunch, as the Setup Assistant install path documents),
    then re-verify the code signature. Quarantine stripping is best-effort, but a
    failed signature check RAISES: the swapped bundle is corrupt or unverified, so
    the caller must drop it rather than relaunch a bad bundle."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(dest)], check=False, timeout=30)
    except Exception as e:  # noqa: BLE001 - quarantine strip is best-effort
        logger.warning(f"whole-app OTA: could not de-quarantine {dest}: {e}")
    # Re-verify post quarantine-strip; raises on a bad signature.
    _verify_app_signature(dest)


def _verify_app_signature(app: Path) -> None:
    """Raise if ``app`` fails codesign --verify (darwin only). Verified on the
    STAGED bundle before it replaces the live app, so a bad signature never
    overwrites a good install (no rollback exists once _install_app replaces it),
    and again after the swap as defense-in-depth."""
    if sys.platform != "darwin":
        return
    subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app)],
        check=True,
        capture_output=True,
        timeout=60,
    )


def swap_changed_ui_apps(
    staging: Path, install_root: Path, old_apps: dict[str, str], new_apps: dict[str, str]
) -> list[str]:
    """Replace UI apps whose content hash changed between the installed and new
    manifests (whole-app OTA). Unchanged apps are left running as-is.
    Returns the keys that were swapped."""
    swapped: list[str] = []
    for key, new_hash in new_apps.items():
        if old_apps.get(key) == new_hash:
            continue  # unchanged; don't disturb the running app
        try:
            name = _ui_app_payload_name(key)
            src = _locate_in_payload(staging, name)
            if src is None:
                logger.warning(f"whole-app OTA: '{key}' changed but '{name}' not in payload; skipping")
                continue
            dests = _ui_app_destinations(key, install_root)
            if not dests:
                continue
            # Verify the staged bundle BEFORE any destructive replace, so a bad
            # signature never overwrites the good installed app.
            _verify_app_signature(src)
            ok_any = False
            for dest in dests:
                try:
                    _install_app(src, dest)
                    _harden_swapped_app(dest)
                    ok_any = True
                except Exception as e:  # noqa: BLE001 - one destination failing shouldn't skip the rest
                    detail = getattr(e, "stderr", None)
                    if isinstance(detail, (bytes, bytearray)):
                        detail = detail.decode(errors="replace")
                    suffix = f" | {detail.strip()}" if detail else ""
                    logger.error(f"whole-app OTA: failed to update '{key}' at {dest}: {e}{suffix}")
            if ok_any:
                swapped.append(key)
                logger.info(f"whole-app OTA: updated '{key}'")
        except Exception as e:  # noqa: BLE001 - one app failing shouldn't abort the rest
            logger.error(f"whole-app OTA: failed to update '{key}': {e}")
    return swapped


def swap_bundle(install_root: Path | None = None) -> bool:
    """Run the full bundle OTA chain end-to-end. Returns True if current was flipped.

    Discovers ``install_root`` if not given, identifies the live version from
    its manifest, asks GitHub for the latest matching release. Returns
    ``False`` without I/O when already at latest. Otherwise: download,
    SHA256 verify (against the release-wide ``checksums.txt`` when present,
    else the per-asset ``.sha256`` sidecar), extract, codesign on macOS,
    health-check the new runtime, atomic flip, GC.

    Failures along the way raise the matching ``BundleUpdateError`` subclass
    so the caller can log and exit. On health-check failure the staged
    version directory is removed before the exception propagates.
    """
    if install_root is None:
        install_root = discover_install_root()
    manifest = read_manifest(install_root)
    release = latest_release_for(manifest.asset_name)

    if release.version == manifest.version:
        logger.info(f"swap_bundle: already at latest ({manifest.version})")
        return False

    logger.info(f"swap_bundle: {manifest.version} -> {release.version}")
    staging = staging_path(install_root)
    archive = download(release.download_url, staging / release.asset_name)
    # checksums.txt is the current release format; the per-asset .sha256
    # sidecar stays as the fallback until the fleet is past the transition.
    expected_sha256: str | None = None
    if release.checksums_url:
        try:
            expected_sha256 = _sha256_from_checksums(release.checksums_url, release.asset_name)
        except VerifyFailed as exc:
            logger.warning(f"swap_bundle: {exc}; falling back to .sha256 sidecar")
    if expected_sha256:
        verify(archive, expected_sha256=expected_sha256, platform=sys.platform)
    else:
        verify(archive, expected_sha256_url=release.sha256_url, platform=sys.platform)

    # Extract once into a work dir we keep, so both the runtime payload and the
    # UI apps can be pulled from it before cleanup.
    extract_dir = staging / "extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    _extract_archive(archive, extract_dir)

    payload = _locate_versioned_payload(extract_dir)
    target = install_root / VERSIONS_DIR / release.version
    if target.exists():
        shutil.rmtree(target)
    os.replace(payload, target)  # moves the runtime out; UI apps stay in extract_dir
    verify_extracted_macos(target)  # no-op on non-macOS

    if not health_check(target / RUNTIME_BINARY):
        shutil.rmtree(target, ignore_errors=True)
        raise HealthCheckFailed(f"self-check failed for staged version {release.version}; rolled back")

    flip_current(install_root, release.version)
    # Stamp the install for the launcher's post-update health window. If
    # the runtime spawned from the new version exits nonzero within the
    # window, the launcher rolls current back to the version recorded here.
    _write_update_pending(install_root, previous_version=manifest.version)

    # Whole-app OTA: after the runtime flip, replace any UI app
    # whose content hash changed, then relaunch it. Never let an app-swap
    # hiccup fail the runtime update that already succeeded.
    try:
        new_apps = read_manifest(install_root).apps  # current now points at target
        swapped = swap_changed_ui_apps(extract_dir, install_root, manifest.apps, new_apps)
        for key in swapped:
            _restart_ui_app(key)
    except Exception as e:  # noqa: BLE001
        logger.error(f"whole-app OTA: UI app swap failed (runtime still updated): {e}")

    # Devices that only ever OTA-update never run the pkg/uninstaller scripts, so
    # sweep the merged-away Setup Assistant here too. See LEGACY-SA-CLEANUP.
    _remove_legacy_setup_assistant(install_root)

    gc_old_versions(install_root)
    clear_staging(install_root)
    logger.info(f"swap_bundle: flipped current -> {release.version}")
    return True


def _write_update_pending(install_root: Path, *, previous_version: str) -> None:
    """Drop the ``.update-pending`` stamp the launcher reads on child exit.

    Two-line plain-text format so the Rust launcher can parse it without
    pulling in a JSON dep::

        <unix_timestamp_seconds>
        <previous_version>
    """
    stamp = install_root / UPDATE_PENDING_STAMP
    body = f"{int(time.time())}\n{previous_version}\n"
    tmp = stamp.with_name(stamp.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, stamp)
