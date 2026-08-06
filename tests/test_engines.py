# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""On-demand engine provisioning: correct per-engine cache dir, binary resolution,
and install-root override. The store fetch itself is mocked (covered by
test_artifact_store); here we test the mapping + resolution."""

from __future__ import annotations

import pytest

from link.app import engines


def _fake_ensure(binary_name="llama-server"):
    def _inner(name, dest_dir=None, base=None, **_kw):
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / binary_name).write_text("fake", encoding="utf-8")
        return dest_dir

    return _inner


def test_provision_uses_per_engine_dir(tmp_path, monkeypatch):
    seen = {}

    def _capture(name, dest_dir=None, base=None, **_kw):
        seen.update(name=name, dest=dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(engines.artifact_store, "ensure_engine", _capture)
    d = engines.provision("llama-cpp", install_root=tmp_path)
    assert d == tmp_path / "engines" / "llama-cpp"  # own dir (no clobber with llama-swap)
    assert seen["name"] == "llama-cpp" and seen["dest"] == d


def test_binary_path_resolves_server(tmp_path, monkeypatch):
    monkeypatch.setattr(engines.artifact_store, "ensure_engine", _fake_ensure("whisper-server"))
    bp = engines.binary_path("whisper-cpp", "whisper-server", install_root=tmp_path)
    assert bp == tmp_path / "engines" / "whisper-cpp" / "whisper-server"


def test_binary_path_raises_when_missing(tmp_path, monkeypatch):
    # ensure_engine "succeeds" but drops no recognised binary.
    monkeypatch.setattr(engines.artifact_store, "ensure_engine", _fake_ensure("not-a-server"))
    with pytest.raises(engines.artifact_store.ArtifactStoreError):
        engines.binary_path("llama-cpp", "llama-server", install_root=tmp_path)


def test_install_root_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAI_INSTALL_ROOT", str(tmp_path / "root"))
    assert engines.engine_cache_root() == tmp_path / "root" / "engines"
