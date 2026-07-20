# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Whole-app OTA against real .app bundles on a real macOS filesystem.

Real ditto / os.replace / Info.plist reads, not the mocked swap in test_updater.
The assertion no other test makes: after an OTA, the .app on disk reports the
new version. Marked ``ci`` (skipped by the default ``-m 'not ci'`` run), darwin
only. Run with ``pytest -m ci tests/test_ota_e2e_macos.py`` on a macOS runner.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import plistlib
import socketserver
import tarfile
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from link.app import updater

pytestmark = [
    pytest.mark.ci,
    pytest.mark.skipif(
        __import__("sys").platform != "darwin",
        reason="real ditto + .app swap semantics are macOS-only",
    ),
]

COMPANION_APP = "Locai Link.app"
# Tauri names the binary after the cargo package, not the productName; the plist
# and updater target this (INFRA-374 fix).
COMPANION_EXEC = "locai-link-companion"
SA_APP = "Setup Assistant.app"


# ---------------------------------------------------------------------------
# Fixtures: build real (minimal) .app bundles and OTA payloads on disk
# ---------------------------------------------------------------------------


def _make_app(path: Path, *, version: str, executable: str, marker: str) -> None:
    """Write a minimal but structurally-real .app bundle."""
    macos = path / "Contents" / "MacOS"
    resources = path / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    info = {
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleExecutable": executable,
        "CFBundleIdentifier": "uk.co.locai.link.companion",
    }
    (path / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    exe = macos / executable
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    # A frontend marker so we can tell old vs new even at equal versions.
    (resources / "build-marker.txt").write_text(marker, encoding="utf-8")


def _app_version(path: Path) -> str:
    data = plistlib.loads((path / "Contents" / "Info.plist").read_bytes())
    return str(data["CFBundleShortVersionString"])


def _app_marker(path: Path) -> str:
    return (path / "Contents" / "Resources" / "build-marker.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The swap function that actually failed on real hardware
# ---------------------------------------------------------------------------


def test_whole_app_ota_swaps_changed_companion_on_disk(tmp_path):
    """Companion hash changed → the install-root .app is replaced with the new
    build; an unchanged app is left untouched."""
    install_root = tmp_path / "Library" / "Locai"
    install_root.mkdir(parents=True)
    _make_app(install_root / COMPANION_APP, version="1.0.0", executable=COMPANION_EXEC, marker="old-companion")
    _make_app(install_root / SA_APP, version="1.0.0", executable="Setup Assistant", marker="old-sa")

    staging = tmp_path / "extracted"
    staging.mkdir()
    _make_app(staging / COMPANION_APP, version="1.1.0", executable=COMPANION_EXEC, marker="new-companion")
    _make_app(staging / SA_APP, version="1.1.0", executable="Setup Assistant", marker="new-sa")

    old_apps = {"companion": "hash-c-old", "setup_assistant": "hash-s-same"}
    new_apps = {"companion": "hash-c-new", "setup_assistant": "hash-s-same"}  # only companion changed

    swapped = updater.swap_changed_ui_apps(staging, install_root, old_apps, new_apps)

    assert swapped == ["companion"]
    # THE assertion: the UI on disk is now the new build.
    assert _app_version(install_root / COMPANION_APP) == "1.1.0"
    assert _app_marker(install_root / COMPANION_APP) == "new-companion"
    # Unchanged app must not be disturbed.
    assert _app_version(install_root / SA_APP) == "1.0.0"
    assert _app_marker(install_root / SA_APP) == "old-sa"
    # No stray temp/backup artifacts left behind.
    assert not (install_root / f".{COMPANION_APP}.new").exists()
    assert not (install_root / f".{COMPANION_APP}.old").exists()


def test_swapped_companion_is_exactly_the_launchd_target(tmp_path):
    """The bundle the OTA writes must be the one the companion LaunchAgent runs.
    Parse the shipped plist, retarget its /Library/Locai path onto our temp
    install root, and confirm that exact binary exists post-swap and is new."""
    repo_root = Path(__file__).resolve().parents[1]
    plist = plistlib.loads(
        (repo_root / "bundling" / "pkg" / "LaunchAgents" / "uk.co.locai.link.companion.plist").read_bytes()
    )
    launched = Path(plist["ProgramArguments"][0])
    rel = launched.relative_to("/Library/Locai")  # e.g. "Locai Link.app/Contents/MacOS/locai-link-companion"

    install_root = tmp_path / "Library" / "Locai"
    install_root.mkdir(parents=True)
    _make_app(install_root / COMPANION_APP, version="1.0.0", executable=COMPANION_EXEC, marker="old")

    staging = tmp_path / "extracted"
    staging.mkdir()
    _make_app(staging / COMPANION_APP, version="1.1.0", executable=COMPANION_EXEC, marker="new")

    updater.swap_changed_ui_apps(staging, install_root, {"companion": "a"}, {"companion": "b"})

    launched_binary = install_root / rel
    assert launched_binary.exists(), f"launchd would run {launched_binary}, which the swap didn't produce"
    assert _app_version(install_root / rel.parts[0]) == "1.1.0"


# ---------------------------------------------------------------------------
# Full swap_bundle chain: download → verify → extract → flip → UI swap
# ---------------------------------------------------------------------------


@contextmanager
def _serve_release(*, repo: str, asset: str, tar_bytes: bytes, version: str):
    """Stand in for GitHub's 'latest release' + asset + sha, over loopback."""
    sha_name = f"{asset}.sha256"
    sha_body = f"{hashlib.sha256(tar_bytes).hexdigest()}  {asset}\n".encode()

    routes: dict[str, tuple[bytes, str]] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a):  # quiet
            pass

        def do_GET(self):  # noqa: N802
            hit = routes.get(self.path)
            if hit is None:
                self.send_error(404)
                return
            body, ctype = hit
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    # Bind once on port 0 — no gap between picking a free port and binding it.
    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    latest = json.dumps(
        {
            "tag_name": f"v{version}",
            "assets": [
                {"name": asset, "browser_download_url": f"{base}/{asset}"},
                {"name": sha_name, "browser_download_url": f"{base}/{sha_name}"},
            ],
        }
    ).encode()
    routes.update(
        {
            f"/repos/{repo}/releases/latest": (latest, "application/json"),
            f"/{asset}": (tar_bytes, "application/gzip"),
            f"/{sha_name}": (sha_body, "text/plain"),
        }
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _write_manifest(path: Path, *, version: str, asset_name: str, apps: dict[str, str]) -> None:
    path.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "asset_name": asset_name,
                "version": version,
                "git_sha": "test",
                "built_at": "test",
                "plugins": [],
                "apps": apps,
            }
        ),
        encoding="utf-8",
    )


def test_swap_bundle_end_to_end_updates_runtime_and_ui(tmp_path, monkeypatch):
    """The full OTA chain against a local 'release', asserting the runtime flips
    AND both UI apps land on the new version at the install root."""
    stem = "locai-link-llm-stt"
    repo = "locai-co-uk/locai-link"
    old_v, new_v = "1.0.0", "1.1.0"
    asset = f"{stem}-macos-arm64-v{new_v}.tar.gz"

    # --- installed root (old) ---------------------------------------------
    install_root = tmp_path / "Library" / "Locai"
    old_ver_dir = install_root / "versions" / old_v
    old_ver_dir.mkdir(parents=True)
    (old_ver_dir / updater.RUNTIME_BINARY).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (old_ver_dir / updater.RUNTIME_BINARY).chmod(0o755)
    old_apps = {"companion": "c-old", "setup_assistant": "s-old"}
    _write_manifest(old_ver_dir / "manifest.json", version=old_v, asset_name=stem, apps=old_apps)
    (install_root / "current").symlink_to(Path("versions") / old_v, target_is_directory=True)
    _make_app(install_root / COMPANION_APP, version=old_v, executable=COMPANION_EXEC, marker="old-companion")
    _make_app(install_root / SA_APP, version=old_v, executable="Setup Assistant", marker="old-sa")

    # --- new OTA tarball (matches release.yml layout) ---------------------
    ota_root = tmp_path / "ota"
    new_ver_dir = ota_root / "versions" / new_v
    new_ver_dir.mkdir(parents=True)
    (new_ver_dir / updater.RUNTIME_BINARY).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (new_ver_dir / updater.RUNTIME_BINARY).chmod(0o755)
    new_apps = {"companion": "c-new", "setup_assistant": "s-new"}
    _write_manifest(new_ver_dir / "manifest.json", version=new_v, asset_name=stem, apps=new_apps)
    _make_app(ota_root / COMPANION_APP, version=new_v, executable=COMPANION_EXEC, marker="new-companion")
    _make_app(ota_root / SA_APP, version=new_v, executable="Setup Assistant", marker="new-sa")

    tar_path = tmp_path / asset
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(ota_root, arcname=".")
    tar_bytes = tar_path.read_bytes()

    # codesign verification of the stub runtime is out of scope here (it's
    # covered directly in test_updater); the UI-swap behaviour is what we're
    # validating end to end.
    monkeypatch.setattr(updater, "verify_extracted_macos", lambda d: None)
    # Don't actually poke launchd on the runner.
    restarts: list[str] = []
    monkeypatch.setattr(updater, "_restart_ui_app", lambda key: restarts.append(key))

    with _serve_release(repo=repo, asset=asset, tar_bytes=tar_bytes, version=new_v) as base:
        monkeypatch.setenv("LOCAI_RELEASES_API_BASE", base)
        monkeypatch.setenv("LOCAI_RELEASES_REPO", repo)
        flipped = updater.swap_bundle(install_root=install_root)

    assert flipped is True
    # Runtime advanced.
    assert updater.read_manifest(install_root).version == new_v
    # THE assertion: both UI apps on disk are the new build.
    assert _app_version(install_root / COMPANION_APP) == new_v
    assert _app_marker(install_root / COMPANION_APP) == "new-companion"
    assert _app_version(install_root / SA_APP) == new_v
    assert _app_marker(install_root / SA_APP) == "new-sa"
    # Companion was asked to relaunch.
    assert "companion" in restarts
