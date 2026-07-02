# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ``src/link/app/updater.py`` — both source-install and bundle OTA paths."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import socketserver
import stat
import subprocess
import sys
import tarfile
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from link.app import updater
from link.app.updater import (
    BundleUpdateError,
    DownloadFailed,
    ExtractRefused,
    InstallRootNotFound,
    Manifest,
    ManifestMalformed,
    ReleaseInfo,
    ReleaseNotFound,
    VerifyFailed,
)
from link.config.models import AgentConfig

# ===========================================================================
# Source-install OTA tests
# ===========================================================================


def _config_with_types(*types: str) -> AgentConfig:
    """Build a minimal AgentConfig whose pipelines reference the given component types."""
    pipelines = [{"id": f"p{i}", "source": {"type": t}, "sink": {"type": "command"}} for i, t in enumerate(types)]
    return AgentConfig.model_validate({"version": 2.1, "identity": {"device_id": "dev"}, "pipelines": pipelines})


def _write_plugin(plugins_dir, name: str, entry_point_type: str | None = None):
    """Create a fake plugin dir with install.py and optional pyproject entry-point."""
    pdir = plugins_dir / name
    pdir.mkdir(parents=True)
    (pdir / "install.py").write_text(f"# {name}")
    if entry_point_type:
        (pdir / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            f'[project.entry-points."locai.plugins"]\n{entry_point_type} = "x:Y"\n'
        )


# --- get_local_version ---


def test_get_local_version(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
    assert updater.get_local_version(tmp_path) == "1.2.3"


def test_get_local_version_missing(tmp_path):
    assert updater.get_local_version(tmp_path) is None


def test_get_local_version_no_version_line(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    assert updater.get_local_version(tmp_path) is None


# --- get_current_branch ---


def test_get_current_branch_success(tmp_path, mocker):
    mock_run = mocker.patch("link.app.updater.subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="dev\n", stderr="")

    assert updater.get_current_branch(tmp_path) == "dev"


def test_get_current_branch_detached_head(tmp_path, mocker):
    mock_run = mocker.patch("link.app.updater.subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="HEAD\n", stderr="")

    assert updater.get_current_branch(tmp_path) is None


def test_get_current_branch_failure(tmp_path, mocker):
    mock_run = mocker.patch("link.app.updater.subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess([], 128, stdout="", stderr="not a repo")

    assert updater.get_current_branch(tmp_path) is None


# --- pull_and_update ---


def test_pull_up_to_date(tmp_path, mocker):
    mocker.patch("link.app.updater._command_exists", return_value=True)
    mocker.patch("link.app.updater.get_current_branch", return_value="main")

    def fake_run(cmd, **kwargs):
        # fetch: succeeds silently
        if cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        # rev-list: 0 commits behind
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    mocker.patch("link.app.updater.subprocess.run", side_effect=fake_run)

    assert updater.pull_and_update(tmp_path) is False


def test_pull_behind_clean_tree(tmp_path, mocker):
    mocker.patch("link.app.updater._command_exists", return_value=True)
    mocker.patch("link.app.updater.get_current_branch", return_value="main")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="3\n", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mocker.patch("link.app.updater.subprocess.run", side_effect=fake_run)

    assert updater.pull_and_update(tmp_path) is True

    # Verify key commands were run in order: fetch, rev-list, status, pull, uv install
    fetch_called = any(c[:2] == ["git", "fetch"] for c in calls)
    pull_called = any(c[:2] == ["git", "pull"] for c in calls)
    uv_install_called = any(c[:4] == ["uv", "pip", "install", "-e"] for c in calls)
    stash_called = any("stash" in c for c in calls)

    assert fetch_called
    assert pull_called
    assert uv_install_called
    assert not stash_called, "Should not stash a clean tree"


def test_pull_behind_dirty_tree_stashes(tmp_path, mocker):
    mocker.patch("link.app.updater._command_exists", return_value=True)
    mocker.patch("link.app.updater.get_current_branch", return_value="main")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="2\n", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=" M file.py\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mocker.patch("link.app.updater.subprocess.run", side_effect=fake_run)

    assert updater.pull_and_update(tmp_path) is True

    stash_push = any(c[:4] == ["git", "stash", "push", "--include-untracked"] for c in calls)
    stash_pop = any(c[:3] == ["git", "stash", "pop"] for c in calls)
    assert stash_push and stash_pop


def test_pull_stash_failure_raises(tmp_path, mocker):
    mocker.patch("link.app.updater._command_exists", return_value=True)
    mocker.patch("link.app.updater.get_current_branch", return_value="main")

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=" M file.py\n", stderr="")
        if cmd[:3] == ["git", "stash", "push"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="conflict")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mocker.patch("link.app.updater.subprocess.run", side_effect=fake_run)

    with pytest.raises(RuntimeError, match="stash"):
        updater.pull_and_update(tmp_path)


def test_pull_no_git_raises(tmp_path, mocker):
    mocker.patch("link.app.updater._command_exists", return_value=False)

    with pytest.raises(RuntimeError, match="git is required"):
        updater.pull_and_update(tmp_path)


def test_pull_uses_current_branch_over_default(tmp_path, mocker):
    """On a dev branch, the update should pull from origin/dev, not origin/main."""
    mocker.patch("link.app.updater._command_exists", return_value=True)
    mocker.patch("link.app.updater.get_current_branch", return_value="dev")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mocker.patch("link.app.updater.subprocess.run", side_effect=fake_run)

    updater.pull_and_update(tmp_path, branch="main")

    # Fetch should target dev, not main
    fetch_cmd = next(c for c in calls if c[:2] == ["git", "fetch"])
    assert fetch_cmd[2:] == ["origin", "dev"]


# --- reinstall_plugin_binaries ---


def test_reinstall_plugin_binaries_no_plugins_dir(tmp_path, mocker):
    mock_run = mocker.patch("link.app.updater.subprocess.run")
    updater.reinstall_plugin_binaries(tmp_path, _config_with_types("alpha"))
    mock_run.assert_not_called()


def test_reinstall_plugin_binaries_runs_only_referenced_plugins(tmp_path, mocker):
    plugins = tmp_path / "plugins"
    _write_plugin(plugins, "alpha", entry_point_type="alpha")
    _write_plugin(plugins, "bravo", entry_point_type="bravo")
    (plugins / "no_installer").mkdir(parents=True)  # Should be skipped (no install.py)

    mock_run = mocker.patch("link.app.updater.subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess([], 0)

    # Config only references "alpha" — bravo should be skipped even though it has install.py
    updater.reinstall_plugin_binaries(tmp_path, _config_with_types("alpha"))

    assert mock_run.call_count == 1
    called_scripts = [str(call.args[0][-1]) for call in mock_run.call_args_list]
    assert any("alpha" in s for s in called_scripts)
    assert not any("bravo" in s for s in called_scripts)


def test_reinstall_plugin_skips_plugin_without_pyproject(tmp_path, mocker):
    """A plugin with no pyproject.toml has no declared entry points → always skipped."""
    plugins = tmp_path / "plugins"
    _write_plugin(plugins, "orphan", entry_point_type=None)  # no pyproject

    mock_run = mocker.patch("link.app.updater.subprocess.run")
    updater.reinstall_plugin_binaries(tmp_path, _config_with_types("orphan"))
    mock_run.assert_not_called()


def test_reinstall_plugin_continues_on_failure(tmp_path, mocker):
    """One plugin failing should not stop the others."""
    plugins = tmp_path / "plugins"
    _write_plugin(plugins, "alpha", entry_point_type="alpha")
    _write_plugin(plugins, "bravo", entry_point_type="bravo")

    def fake_run(cmd, **kwargs):
        if "alpha" in str(cmd):
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    mocker.patch("link.app.updater.subprocess.run", side_effect=fake_run)

    # Should not raise
    updater.reinstall_plugin_binaries(tmp_path, _config_with_types("alpha", "bravo"))


# ===========================================================================
# Bundle OTA tests — exercise the in-process http.server, real tarballs/zips,
# real subprocesses. No requests-mock dependency.
# ===========================================================================


def _write_manifest(version_dir: Path, *, version: str, asset_name: str = "locai-link-llm-linux-x86_64") -> None:
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / updater.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "asset_name": asset_name,
                "version": version,
                "git_sha": "deadbee",
                "built_at": "2026-06-19T00:00:00Z",
                "plugins": [{"name": "language_model", "version": "0.1.0"}],
            }
        )
    )


def _setup_install_root(tmp_path: Path, version: str = "1.0.15") -> Path:
    """Build a minimal install_root with one version + symlink current."""
    version_dir = tmp_path / updater.VERSIONS_DIR / version
    _write_manifest(version_dir, version=version)
    (tmp_path / updater.CURRENT_LINK).symlink_to(Path(updater.VERSIONS_DIR) / version, target_is_directory=True)
    return tmp_path


@contextmanager
def _serve_dir(directory: Path):
    """Tiny HTTP server over a directory. Yields the base URL."""

    handler_cls = type(
        "QuietHandler",
        (http.server.SimpleHTTPRequestHandler,),
        {
            "log_message": lambda *_args, **_kwargs: None,
            # SimpleHTTPRequestHandler resolves paths relative to cwd; we set
            # directory= via a partial-style subclass instead.
            "__init__": lambda self, *a, **kw: http.server.SimpleHTTPRequestHandler.__init__(
                self, *a, directory=str(directory), **kw
            ),
        },
    )
    server = socketserver.TCPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# --- discover_install_root / read_manifest / read_boot_config ---


def test_discover_install_root_via_current_symlink(tmp_path):
    root = _setup_install_root(tmp_path)
    runtime = root / updater.VERSIONS_DIR / "1.0.15" / updater.RUNTIME_BINARY
    runtime.touch()
    assert updater.discover_install_root(runtime) == root


def test_discover_install_root_via_versions_dir(tmp_path):
    # No current pointer yet — first install before flip — still discoverable
    # because versions/ exists.
    (tmp_path / updater.VERSIONS_DIR / "1.0.0").mkdir(parents=True)
    deeper = tmp_path / updater.VERSIONS_DIR / "1.0.0" / "_internal" / "x"
    deeper.mkdir(parents=True)
    assert updater.discover_install_root(deeper) == tmp_path


def test_discover_install_root_raises_when_absent(tmp_path):
    with pytest.raises(InstallRootNotFound):
        updater.discover_install_root(tmp_path / "deep" / "nothing")


def test_read_manifest_happy_path(tmp_path):
    root = _setup_install_root(tmp_path, version="1.2.3")
    m = updater.read_manifest(root)
    assert isinstance(m, Manifest)
    assert m.version == "1.2.3"
    assert m.asset_name.startswith("locai-link-")


def test_read_manifest_no_current(tmp_path):
    with pytest.raises(InstallRootNotFound):
        updater.read_manifest(tmp_path)


def test_read_manifest_malformed(tmp_path):
    version_dir = tmp_path / updater.VERSIONS_DIR / "1.0.0"
    version_dir.mkdir(parents=True)
    (version_dir / updater.MANIFEST_NAME).write_text("not json")
    (tmp_path / updater.CURRENT_LINK).symlink_to(Path(updater.VERSIONS_DIR) / "1.0.0", target_is_directory=True)
    with pytest.raises(ManifestMalformed):
        updater.read_manifest(tmp_path)


def test_read_manifest_missing_field(tmp_path):
    version_dir = tmp_path / updater.VERSIONS_DIR / "1.0.0"
    version_dir.mkdir(parents=True)
    (version_dir / updater.MANIFEST_NAME).write_text(json.dumps({"manifest_version": 1}))
    (tmp_path / updater.CURRENT_LINK).symlink_to(Path(updater.VERSIONS_DIR) / "1.0.0", target_is_directory=True)
    with pytest.raises(ManifestMalformed):
        updater.read_manifest(tmp_path)


def test_read_boot_config_present(tmp_path):
    (tmp_path / updater.BOOT_NAME).write_text(
        json.dumps(
            {
                "host_app": "Meetily",
                "plugin_set": ["llm"],
                "channel": "stable",
                "asset_repo": "locai-co-uk/locai-link",
            }
        )
    )
    cfg = updater.read_boot_config(tmp_path)
    assert cfg is not None
    assert cfg.host_app == "Meetily"


def test_read_boot_config_absent(tmp_path):
    assert updater.read_boot_config(tmp_path) is None


# --- latest_release_for ---


def _fake_release_payload(stem: str, version: str) -> dict[str, Any]:
    return {
        "tag_name": f"v{version}",
        "assets": [
            {
                "name": f"{stem}-v{version}.tar.gz",
                "browser_download_url": f"https://example/{stem}-v{version}.tar.gz",
            },
            {
                "name": f"{stem}-v{version}.tar.gz.sha256",
                "browser_download_url": f"https://example/{stem}-v{version}.tar.gz.sha256",
            },
            {
                "name": f"locai-link-other-linux-x86_64-v{version}.tar.gz",
                "browser_download_url": "https://example/other.tar.gz",
            },
        ],
    }


class _StubSession:
    """A drop-in for ``requests.Session`` that returns canned responses by URL."""

    def __init__(self, responses: dict[str, tuple[int, bytes] | dict[str, Any]]):
        self._responses = responses

    def get(self, url, *, timeout=None, headers=None, stream=False, **kwargs):  # noqa: D401
        payload = self._responses[url]
        if isinstance(payload, dict):
            # JSON shape for latest_release_for
            return _StubResponse(200, json_body=payload)
        status, body = payload
        return _StubResponse(status, body=body)


class _StubResponse:
    def __init__(self, status: int, body: bytes = b"", json_body: dict[str, Any] | None = None):
        self.status_code = status
        self._body = body
        self._json = json_body
        self.text = body.decode("utf-8", errors="replace")
        self.headers = {"Content-Length": str(len(body))}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise __import__("requests").HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    # context-manager protocol (used by download())
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size: int):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


def test_latest_release_for_picks_matching_asset():
    stem = "locai-link-llm-linux-x86_64"
    payload = _fake_release_payload(stem, "1.0.16")
    session = _StubSession({"https://api.github.com/repos/foo/bar/releases/latest": payload})
    info = updater.latest_release_for(stem, repo="foo/bar", session=session)
    assert isinstance(info, ReleaseInfo)
    assert info.version == "1.0.16"
    assert info.tag == "v1.0.16"
    assert info.asset_name == f"{stem}-v1.0.16.tar.gz"
    assert info.sha256_url is not None and info.sha256_url.endswith(".sha256")


def test_latest_release_for_no_matching_asset():
    stem = "locai-link-llm-linux-x86_64"
    payload = {
        "tag_name": "v9.9.9",
        "assets": [{"name": "something-else.tar.gz", "browser_download_url": "x"}],
    }
    session = _StubSession({"https://api.github.com/repos/foo/bar/releases/latest": payload})
    with pytest.raises(ReleaseNotFound):
        updater.latest_release_for(stem, repo="foo/bar", session=session)


# --- download ---


def test_download_writes_file_and_renames(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    payload = os.urandom(3 * 1024 * 1024)  # 3 MiB, ensures multiple chunks
    (src_dir / "asset.bin").write_bytes(payload)

    progress_calls: list[tuple[int, int]] = []

    def on_progress(done, total):
        progress_calls.append((done, total))

    with _serve_dir(src_dir) as base_url:
        dest = tmp_path / "out" / "asset.bin"
        out = updater.download(f"{base_url}/asset.bin", dest, progress=on_progress)
        assert out == dest
        assert dest.read_bytes() == payload

    # Partial file should be gone after successful rename.
    assert not dest.with_suffix(dest.suffix + ".partial").exists()
    # At least one progress callback was emitted.
    assert progress_calls
    # Final progress should be >= total size.
    assert progress_calls[-1][0] == len(payload)


def test_download_retries_then_raises_on_persistent_failure(tmp_path):
    dest = tmp_path / "out" / "asset.bin"

    class FailingSession:
        def get(self, *a, **kw):
            import requests

            raise requests.ConnectionError("network down")

    with pytest.raises(DownloadFailed):
        updater.download(
            "https://nope.invalid/asset.bin",
            dest,
            session=FailingSession(),  # type: ignore[arg-type]
            max_retries=2,
        )


# --- verify ---


def test_verify_sha_match(tmp_path):
    payload = b"hello world"
    target = tmp_path / "asset.bin"
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    updater.verify(target, expected_sha256=digest)  # no raise


def test_verify_sha_mismatch(tmp_path):
    target = tmp_path / "asset.bin"
    target.write_bytes(b"hello world")
    with pytest.raises(VerifyFailed):
        updater.verify(target, expected_sha256="00" * 32)


def test_verify_no_expected_raises(tmp_path):
    target = tmp_path / "asset.bin"
    target.write_bytes(b"x")
    with pytest.raises(VerifyFailed):
        updater.verify(target)


def test_verify_fetches_sha_from_url(tmp_path):
    payload = b"hello world"
    target = tmp_path / "asset.bin"
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    # sha256sum-style: "<hex>  <filename>"
    sha_body = f"{digest}  asset.bin\n".encode()
    session = _StubSession({"https://example/asset.sha256": (200, sha_body)})
    updater.verify(target, expected_sha256_url="https://example/asset.sha256", session=session)  # type: ignore[arg-type]


# --- extract ---


def _make_tar_with(entries: dict[str, bytes]) -> bytes:
    import io

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_extract_tar_real_versions_wrapped_shape(tmp_path):
    """The actual CI tarball shape: versions/<v>/... plus launcher at root."""
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(
        _make_tar_with(
            {
                "versions/1.0.16/manifest.json": b"{}",
                "versions/1.0.16/runtime": b"binary",
                "locai-link": b"launcher",  # tarball root has launcher too — must be ignored
            }
        )
    )
    dest = tmp_path / "versions" / "1.0.16"
    updater.extract(archive, dest)
    assert (dest / "manifest.json").is_file()
    assert (dest / "runtime").read_bytes() == b"binary"
    # The launcher at the tarball root must NOT land in the extracted bundle dir.
    assert not (dest / "locai-link").exists()


def test_extract_tar_legacy_single_top_level_dir(tmp_path):
    """Fallback shape: a single wrapping dir, no versions/ prefix."""
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(_make_tar_with({"1.0.16/manifest.json": b"{}", "1.0.16/runtime": b"binary"}))
    dest = tmp_path / "versions" / "1.0.16"
    updater.extract(archive, dest)
    assert (dest / "manifest.json").is_file()
    assert (dest / "runtime").read_bytes() == b"binary"


def test_extract_rejects_versions_with_multiple_children(tmp_path):
    """Two version dirs inside one tarball is a build mistake — refuse."""
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(
        _make_tar_with(
            {
                "versions/1.0.16/manifest.json": b"{}",
                "versions/1.0.17/manifest.json": b"{}",
            }
        )
    )
    with pytest.raises(BundleUpdateError):
        updater.extract(archive, tmp_path / "out")


def test_extract_refuses_path_traversal(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    archive.write_bytes(_make_tar_with({"../escape.txt": b"oops"}))
    with pytest.raises(ExtractRefused):
        updater.extract(archive, tmp_path / "versions" / "1.0.0")


def test_extract_refuses_absolute_path(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    archive.write_bytes(_make_tar_with({"/etc/passwd": b"oops"}))
    with pytest.raises(ExtractRefused):
        updater.extract(archive, tmp_path / "versions" / "1.0.0")


def test_extract_zip(tmp_path):
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("1.0.16/manifest.json", "{}")
        zf.writestr("1.0.16/runtime", "binary")
    dest = tmp_path / "versions" / "1.0.16"
    updater.extract(archive, dest)
    assert (dest / "manifest.json").is_file()


# --- flip_current / gc_old_versions ---


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_flip_current_symlink_shape(tmp_path):
    root = _setup_install_root(tmp_path, version="1.0.15")
    _write_manifest(root / updater.VERSIONS_DIR / "1.0.16", version="1.0.16")

    updater.flip_current(root, "1.0.16")

    assert (root / updater.CURRENT_LINK).is_symlink()
    assert os.readlink(root / updater.CURRENT_LINK).endswith("1.0.16")
    assert (root / updater.PREVIOUS_LINK).is_symlink()
    assert os.readlink(root / updater.PREVIOUS_LINK).endswith("1.0.15")


def test_flip_current_pointer_file_shape(tmp_path):
    """Windows-without-Developer-Mode shape: CURRENT/PREVIOUS pointer files."""
    (tmp_path / updater.VERSIONS_DIR / "1.0.15").mkdir(parents=True)
    _write_manifest(tmp_path / updater.VERSIONS_DIR / "1.0.15", version="1.0.15")
    (tmp_path / updater.VERSIONS_DIR / "1.0.16").mkdir(parents=True)
    _write_manifest(tmp_path / updater.VERSIONS_DIR / "1.0.16", version="1.0.16")
    (tmp_path / updater.CURRENT_POINTER_FILE).write_text("1.0.15\n")

    updater.flip_current(tmp_path, "1.0.16")

    assert (tmp_path / updater.CURRENT_POINTER_FILE).read_text().strip() == "1.0.16"
    assert (tmp_path / updater.PREVIOUS_POINTER_FILE).read_text().strip() == "1.0.15"
    # Check is_symlink() not exists(): on case-insensitive filesystems
    # (macOS APFS/HFS+, Windows NTFS by default) the pointer file "CURRENT"
    # and the symlink name "current" resolve to the same directory entry,
    # so `exists()` returns True from the pointer file itself. The
    # invariant we care about is that no *symlink* was created in the
    # pointer-file shape — which is_symlink checks correctly.
    assert not (tmp_path / updater.CURRENT_LINK).is_symlink()


def test_flip_current_missing_target_raises(tmp_path):
    root = _setup_install_root(tmp_path, version="1.0.15")
    with pytest.raises(BundleUpdateError):
        updater.flip_current(root, "9.9.9")


def test_gc_keeps_current_and_previous(tmp_path):
    for v in ("1.0.13", "1.0.14", "1.0.15", "1.0.16"):
        (tmp_path / updater.VERSIONS_DIR / v).mkdir(parents=True)
    (tmp_path / updater.CURRENT_LINK).symlink_to(Path(updater.VERSIONS_DIR) / "1.0.16", target_is_directory=True)
    (tmp_path / updater.PREVIOUS_LINK).symlink_to(Path(updater.VERSIONS_DIR) / "1.0.15", target_is_directory=True)

    removed = updater.gc_old_versions(tmp_path)
    assert set(removed) == {"1.0.13", "1.0.14"}
    assert (tmp_path / updater.VERSIONS_DIR / "1.0.15").is_dir()
    assert (tmp_path / updater.VERSIONS_DIR / "1.0.16").is_dir()


def test_gc_keeps_extra_when_keep_higher(tmp_path):
    for v in ("1.0.13", "1.0.14", "1.0.15"):
        (tmp_path / updater.VERSIONS_DIR / v).mkdir(parents=True)
    (tmp_path / updater.CURRENT_LINK).symlink_to(Path(updater.VERSIONS_DIR) / "1.0.15", target_is_directory=True)
    removed = updater.gc_old_versions(tmp_path, keep=3)
    assert removed == []


# --- health_check ---


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Fixture uses a #!/usr/bin/env bash shebang script which Windows can't exec. "
    "Windows OTA validation exercises health_check with a real locai-link.exe in CI.",
)
def test_health_check_passes_on_exit_zero(tmp_path):
    script = tmp_path / "fake_runtime"
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    assert updater.health_check(script, timeout=5.0) is True


def test_health_check_fails_on_exit_nonzero(tmp_path):
    script = tmp_path / "fake_runtime"
    script.write_text('#!/usr/bin/env bash\necho "boom" >&2\nexit 1\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    assert updater.health_check(script, timeout=5.0) is False


def test_health_check_fails_on_missing_binary(tmp_path):
    assert updater.health_check(tmp_path / "does_not_exist") is False


# --- running_frozen_bundle / swap_bundle ---


def test_running_frozen_bundle_true(mocker):
    mocker.patch.object(updater.sys, "frozen", True, create=True)
    mocker.patch.object(updater.sys, "_MEIPASS", "/tmp/_MEI123", create=True)
    assert updater.running_frozen_bundle() is True


def test_running_frozen_bundle_false_in_source_install(mocker):
    mocker.patch.object(updater.sys, "frozen", False, create=True)
    assert updater.running_frozen_bundle() is False


def _install_root_with_runtime_stub(tmp_path: Path, version: str) -> Path:
    """An install_root whose locai-link-runtime stub exits 0 on `self-check`."""
    root = _setup_install_root(tmp_path, version=version)
    runtime = root / updater.VERSIONS_DIR / version / updater.RUNTIME_BINARY
    runtime.write_text('#!/usr/bin/env bash\n[ "$1" = "self-check" ] && exit 0 || exit 1\n')
    runtime.chmod(runtime.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return root


def test_swap_bundle_short_circuits_when_already_at_latest(tmp_path, mocker):
    root = _install_root_with_runtime_stub(tmp_path, "1.0.15")
    mocker.patch.object(
        updater,
        "latest_release_for",
        return_value=ReleaseInfo(
            version="1.0.15",
            tag="v1.0.15",
            asset_name="locai-link-llm-linux-x86_64-v1.0.15.tar.gz",
            download_url="https://example/x.tar.gz",
            sha256_url="https://example/x.tar.gz.sha256",
        ),
    )
    download_spy = mocker.patch.object(updater, "download")
    flip_spy = mocker.patch.object(updater, "flip_current")

    assert updater.swap_bundle(install_root=root) is False
    download_spy.assert_not_called()
    flip_spy.assert_not_called()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fake_extract lays down a bash-script runtime the Windows loader can't exec; "
    "health_check fails for the wrong reason. Windows OTA is validated separately.",
)
def test_swap_bundle_happy_path(tmp_path, mocker):
    """End-to-end mock: newer release available -> chain runs, current flipped."""
    root = _install_root_with_runtime_stub(tmp_path, "1.0.15")
    new_version = "1.0.16"
    asset_name = f"locai-link-llm-linux-x86_64-v{new_version}.tar.gz"

    mocker.patch.object(
        updater,
        "latest_release_for",
        return_value=ReleaseInfo(
            version=new_version,
            tag=f"v{new_version}",
            asset_name=asset_name,
            download_url="https://example/" + asset_name,
            sha256_url="https://example/" + asset_name + ".sha256",
        ),
    )

    # Stub download + verify so we don't need a network. extract gets a real
    # tarball so the on-disk flip / health-check exercise real code paths.
    def fake_download(url, dest, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"placeholder")
        return dest

    mocker.patch.object(updater, "download", side_effect=fake_download)
    mocker.patch.object(updater, "verify")
    # verify_extracted_macos shells out to `codesign` on macOS runners;
    # the fake bash-script runtime we lay down isn't signed, so mock it.
    mocker.patch.object(updater, "verify_extracted_macos")

    def fake_extract(archive, target):
        # Lay down a manifest + a runnable runtime stub at the target dir.
        target.mkdir(parents=True, exist_ok=True)
        (target / updater.MANIFEST_NAME).write_text("{}")
        runtime = target / updater.RUNTIME_BINARY
        runtime.write_text("#!/usr/bin/env bash\nexit 0\n")
        runtime.chmod(runtime.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    mocker.patch.object(updater, "extract", side_effect=fake_extract)

    assert updater.swap_bundle(install_root=root) is True
    assert (root / updater.CURRENT_LINK).resolve().name == new_version
    assert (root / updater.PREVIOUS_LINK).resolve().name == "1.0.15"

    # Phase 4: the launcher's post-update health window is gated on this stamp.
    stamp = root / updater.UPDATE_PENDING_STAMP
    assert stamp.is_file(), "swap_bundle must write .update-pending after flip"
    lines = stamp.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2
    assert int(lines[0]) > 0, "stamp first line must be a unix timestamp"
    assert lines[1] == "1.0.15", "stamp second line must record the previous version"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fake_extract lays down a bash-script runtime; on Windows the exec fails "
    "before we test the exit-17 path we actually care about (test would pass by "
    "accident for the wrong reason).",
)
def test_swap_bundle_rolls_back_on_health_check_failure(tmp_path, mocker):
    """A failing self-check should remove the staged version and raise."""
    root = _install_root_with_runtime_stub(tmp_path, "1.0.15")
    new_version = "1.0.16"
    asset_name = f"locai-link-llm-linux-x86_64-v{new_version}.tar.gz"

    mocker.patch.object(
        updater,
        "latest_release_for",
        return_value=ReleaseInfo(
            version=new_version,
            tag=f"v{new_version}",
            asset_name=asset_name,
            download_url="https://example/" + asset_name,
            sha256_url="https://example/" + asset_name + ".sha256",
        ),
    )
    mocker.patch.object(updater, "download", side_effect=lambda url, dest, **_: dest)
    mocker.patch.object(updater, "verify")
    # Same as the happy-path test: codesign would reject the fake runtime.
    mocker.patch.object(updater, "verify_extracted_macos")

    def fake_extract(archive, target):
        # A runtime that exits nonzero so health_check fails.
        target.mkdir(parents=True, exist_ok=True)
        runtime = target / updater.RUNTIME_BINARY
        runtime.write_text("#!/usr/bin/env bash\nexit 17\n")
        runtime.chmod(runtime.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    mocker.patch.object(updater, "extract", side_effect=fake_extract)
    flip_spy = mocker.patch.object(updater, "flip_current")

    with pytest.raises(updater.HealthCheckFailed):
        updater.swap_bundle(install_root=root)

    # The staged version dir should be cleaned up on failure.
    assert not (root / updater.VERSIONS_DIR / new_version).exists()
    # And no flip should have happened.
    flip_spy.assert_not_called()
    assert (root / updater.CURRENT_LINK).resolve().name == "1.0.15"
