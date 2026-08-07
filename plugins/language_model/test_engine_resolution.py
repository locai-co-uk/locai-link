# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""resolve_engine_binary contract: bundled dir first; PATH only on source runs;
frozen bundles fall through to a version-pinned artifact-store fetch, and a
fetch failure resolves to None (the caller fails the serve loudly)."""

import sys
import types
from pathlib import Path

import pytest

try:
    from .server import resolve_engine_binary
except ImportError:
    from server import resolve_engine_binary


@pytest.fixture
def engines_stub(monkeypatch):
    """Install a fake link.infra.engines and record binary_path calls."""
    calls: list[dict] = []
    stub = types.ModuleType("link.infra.engines")

    def binary_path(name, binary, *, version=None, **kwargs):
        calls.append({"name": name, "binary": binary, "version": version})
        return Path(f"/store/{name}/{binary}")

    stub.binary_path = binary_path
    infra = types.ModuleType("link.infra")
    infra.engines = stub
    monkeypatch.setitem(sys.modules, "link", types.ModuleType("link"))
    monkeypatch.setitem(sys.modules, "link.infra", infra)
    monkeypatch.setitem(sys.modules, "link.infra.engines", stub)
    return calls


def _freeze(monkeypatch, frozen: bool):
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.delattr(sys, "frozen", raising=False)


def test_bundled_binary_wins(tmp_path, monkeypatch, engines_stub):
    _freeze(monkeypatch, True)
    bundled = tmp_path / "llama-server"
    bundled.touch()
    assert resolve_engine_binary("llama-cpp", "llama-server", tmp_path, version="b1") == bundled
    assert engines_stub == [], "bundled hit must not touch the store"


def test_frozen_skips_path_and_fetches_pinned(tmp_path, monkeypatch, engines_stub):
    _freeze(monkeypatch, True)
    # A PATH binary exists, but frozen resolution must not trust it.
    fake_path_dir = tmp_path / "pathdir"
    fake_path_dir.mkdir()
    exe = fake_path_dir / "llama-server"
    exe.touch()
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_path_dir))

    got = resolve_engine_binary("llama-cpp", "llama-server", tmp_path / "empty", version="b10289")
    assert got == Path("/store/llama-cpp/llama-server")
    assert engines_stub == [{"name": "llama-cpp", "binary": "llama-server", "version": "b10289"}]


def test_source_run_uses_path(tmp_path, monkeypatch, engines_stub):
    _freeze(monkeypatch, False)
    fake_path_dir = tmp_path / "pathdir"
    fake_path_dir.mkdir()
    exe = fake_path_dir / "llama-server"
    exe.touch()
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_path_dir))

    got = resolve_engine_binary("llama-cpp", "llama-server", tmp_path / "empty")
    assert got == exe
    assert engines_stub == [], "source runs never fetch from the store"


def test_source_run_without_path_is_none(tmp_path, monkeypatch, engines_stub):
    _freeze(monkeypatch, False)
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    assert resolve_engine_binary("llama-cpp", "llama-server", tmp_path / "empty") is None
    assert engines_stub == []


def test_frozen_fetch_failure_is_none(tmp_path, monkeypatch, engines_stub):
    _freeze(monkeypatch, True)

    def boom(*args, **kwargs):
        raise RuntimeError("store 404")

    sys.modules["link.infra.engines"].binary_path = boom
    sys.modules["link.infra"].engines.binary_path = boom
    assert resolve_engine_binary("llama-cpp", "llama-server", tmp_path / "empty", version="b10289") is None
