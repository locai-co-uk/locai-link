# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""resolve_engine_binary contract: bundled dir first; PATH only on source runs;
frozen bundles fall through to a version-pinned artifact-store fetch, and a
fetch failure resolves to None (the caller fails the serve loudly)."""

import os
import sys
import types
from pathlib import Path
from typing import NoReturn

import pytest

try:
    from .server import resolve_engine_binary
except ImportError:
    from server import resolve_engine_binary


# shutil.which on Windows only matches PATHEXT executables, so PATH tests
# must use the platform-resolved filename, same as the production callers.
_SERVER = "llama-server.exe" if os.name == "nt" else "llama-server"

# binary_path calls recorded by the engines_stub fixture.
_Calls = list[dict[str, str | None]]


@pytest.fixture
def engines_stub(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    """Install a fake link.infra.engines and record binary_path calls."""
    calls: _Calls = []
    stub = types.ModuleType("link.infra.engines")

    def binary_path(name: str, binary: str, *, version: str | None = None, **kwargs: object) -> Path:
        calls.append({"name": name, "binary": binary, "version": version})
        return Path("/store") / name / binary

    stub.binary_path = binary_path
    infra = types.ModuleType("link.infra")
    infra.engines = stub
    monkeypatch.setitem(sys.modules, "link", types.ModuleType("link"))
    monkeypatch.setitem(sys.modules, "link.infra", infra)
    monkeypatch.setitem(sys.modules, "link.infra.engines", stub)
    return calls


def _freeze(monkeypatch: pytest.MonkeyPatch, frozen: bool) -> None:
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.delattr(sys, "frozen", raising=False)


def test_bundled_binary_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engines_stub: _Calls) -> None:
    _freeze(monkeypatch, True)
    bundled = tmp_path / _SERVER
    bundled.touch()
    assert resolve_engine_binary("llama-cpp", _SERVER, tmp_path, version="b1") == bundled
    assert engines_stub == [], "bundled hit must not touch the store"


def test_frozen_skips_path_and_fetches_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engines_stub: _Calls
) -> None:
    _freeze(monkeypatch, True)
    # A PATH binary exists, but frozen resolution must not trust it.
    fake_path_dir = tmp_path / "pathdir"
    fake_path_dir.mkdir()
    exe = fake_path_dir / _SERVER
    exe.touch()
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_path_dir))

    got = resolve_engine_binary("llama-cpp", _SERVER, tmp_path / "empty", version="b10289")
    assert got == Path("/store") / "llama-cpp" / _SERVER
    assert engines_stub == [{"name": "llama-cpp", "binary": _SERVER, "version": "b10289"}]


def test_source_run_uses_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engines_stub: _Calls) -> None:
    _freeze(monkeypatch, False)
    fake_path_dir = tmp_path / "pathdir"
    fake_path_dir.mkdir()
    exe = fake_path_dir / _SERVER
    exe.touch()
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_path_dir))

    got = resolve_engine_binary("llama-cpp", _SERVER, tmp_path / "empty")
    assert got == exe
    assert engines_stub == [], "source runs never fetch from the store"


def test_source_run_without_path_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engines_stub: _Calls) -> None:
    _freeze(monkeypatch, False)
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    assert resolve_engine_binary("llama-cpp", _SERVER, tmp_path / "empty") is None
    assert engines_stub == []


def test_frozen_fetch_failure_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engines_stub: _Calls) -> None:
    _freeze(monkeypatch, True)

    def boom(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError("store 404")

    sys.modules["link.infra.engines"].binary_path = boom
    sys.modules["link.infra"].engines.binary_path = boom
    assert resolve_engine_binary("llama-cpp", _SERVER, tmp_path / "empty", version="b10289") is None
