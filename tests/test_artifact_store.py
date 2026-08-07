# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Artifact-store client: platform-arch resolution, manifest parse, and an
end-to-end fetch (download -> sha256 verify -> extract) against a local mock
store served over HTTP. The mock store lives entirely in ``tmp_path`` (auto-
cleaned); nothing is written to the repo."""

from __future__ import annotations

import functools
import http.server
import io
import tarfile
import threading

import publish_artifacts as pub
import pytest

from link.infra import artifact_store as store


def _make_engine_archive(dest, binary_name="llama-server", body=b"ELF-fake-binary"):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tf:
        info = tarfile.TarInfo(name=f"build/bin/{binary_name}")
        info.size = len(body)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(body))
    return dest


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    # keep test output clean
    def log_message(self, *_a):  # pyright: ignore[reportIncompatibleMethodOverride, reportImplicitOverride]
        pass


@pytest.fixture
def served_store(tmp_path, monkeypatch):
    """Seed a store from a fixture archive, serve it over HTTP, point the client
    at it via LOCAI_ARTIFACT_BASE. Yields (base_url, store_dir)."""
    src = tmp_path / "src"
    src.mkdir()
    _make_engine_archive(src / "llama-cpp-b10289-linux-x64.tar.gz")
    store_dir = tmp_path / "store"
    pub._from_dir(store_dir, src)  # place artifact + rebuild manifest

    handler = functools.partial(_QuietHandler, directory=str(store_dir))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setenv("LOCAI_ARTIFACT_BASE", base)
    try:
        yield base, store_dir
    finally:
        httpd.shutdown()
        httpd.server_close()  # shutdown() stops serve_forever but keeps the socket
        server_thread.join(timeout=5)


def test_platform_arch(monkeypatch):
    monkeypatch.setattr(store.sys, "platform", "linux")
    monkeypatch.setattr(store.platform, "machine", lambda: "x86_64")
    assert store.platform_arch() == "linux-x64"
    monkeypatch.setattr(store.sys, "platform", "darwin")
    monkeypatch.setattr(store.platform, "machine", lambda: "arm64")
    assert store.platform_arch() == "macos-arm64"
    monkeypatch.setattr(store.sys, "platform", "win32")
    monkeypatch.setattr(store.platform, "machine", lambda: "AMD64")
    assert store.platform_arch() == "windows-x64"
    monkeypatch.setattr(store.platform, "machine", lambda: "sparc64")
    with pytest.raises(store.ArtifactStoreError):
        store.platform_arch()


def test_base_url_env_override(monkeypatch):
    monkeypatch.delenv("LOCAI_ARTIFACT_BASE", raising=False)
    assert store.base_url() == store.DEFAULT_BASE
    monkeypatch.setenv("LOCAI_ARTIFACT_BASE", "http://localhost:9000/store/")
    assert store.base_url() == "http://localhost:9000/store"  # trailing slash stripped


def test_manifest_schema_and_lookup():
    with pytest.raises(store.ManifestError):
        store.Manifest({"schema": 2})
    m = store.Manifest(
        {"schema": 1, "engines": {"llama-cpp": {"b10289": {"linux-x64": {"path": "p/x.tar.gz", "sha256": "abc"}}}}}
    )
    assert m.variant("engines", "llama-cpp", "b10289", "linux-x64").path == "p/x.tar.gz"
    with pytest.raises(store.VariantNotFound):
        m.variant("engines", "llama-cpp", "bZZZZ", "linux-x64")


def test_manifest_default_version():
    m = store.Manifest(
        {
            "schema": 1,
            "defaults": {"engines": {"llama-cpp": "b10289"}},
            "engines": {"llama-cpp": {"b10289": {"linux-x64": {"path": "p", "sha256": "s"}}}},
        }
    )
    assert m.default_version("engines", "llama-cpp") == "b10289"
    # version omitted -> resolves via the default
    assert m.variant("engines", "llama-cpp", arch="linux-x64").path == "p"
    with pytest.raises(store.VariantNotFound):
        m.default_version("engines", "whisper-cpp")


def test_fetch_uses_manifest_default_version(served_store, tmp_path):
    # No version passed: the publish job set a per-engine default, so the client
    # resolves it from the manifest (device does not re-pin versions).
    dest = tmp_path / "engines" / "llama"
    out = store.ensure_engine("llama-cpp", dest_dir=dest, arch="linux-x64")
    assert (out / "llama-server").is_file()


def test_fetch_end_to_end(served_store, tmp_path):
    _, _ = served_store
    dest = tmp_path / "engines" / "llama"
    out = store.ensure_engine("llama-cpp", "b10289", dest, arch="linux-x64")
    assert (out / "llama-server").is_file()
    assert (out / ".artifact-sha256").exists()
    # Idempotent: a second call with the artifact already present is a no-op.
    store.ensure_engine("llama-cpp", "b10289", dest, arch="linux-x64")
    assert (out / "llama-server").read_bytes() == b"ELF-fake-binary"


def test_verify_rejects_tampered(served_store, tmp_path):
    _, store_dir = served_store
    # Corrupt the stored artifact so it no longer matches the manifest's sha256.
    art = next((store_dir / "engines" / "llama-cpp" / "b10289" / "linux-x64").glob("*.tar.gz"))
    art.write_bytes(b"TAMPERED")
    dest = tmp_path / "engines" / "llama"
    with pytest.raises(store.VerificationError):
        store.ensure_engine("llama-cpp", "b10289", dest, arch="linux-x64")
    assert not (dest / "llama-server").exists()  # nothing placed on a failed verify


def test_headless_first_use_chain(served_store, tmp_path, monkeypatch):
    """The full headless first-use path with NOTHING mocked: engines.binary_path
    -> artifact_store -> served mock store -> engine binary on disk. This is what
    the serving path hits when a headless install has no bundled engine."""
    from link.infra import engines

    monkeypatch.setattr(store, "platform_arch", lambda: "linux-x64")
    bp = engines.binary_path("llama-cpp", "llama-server", install_root=tmp_path)
    assert bp == tmp_path / "engines" / "llama-cpp" / "llama-server"
    assert bp.is_file()


def test_variant_candidates_prefer_accel_then_cpu(monkeypatch):
    # A GPU linux box: candidates are [vulkan, cpu], best first.
    monkeypatch.setattr(store.sys, "platform", "linux")
    monkeypatch.setattr(store.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(store, "_has_gpu", lambda: True)
    assert store.variant_candidates() == ["linux-x64-vulkan", "linux-x64"]

    base = {"schema": 1, "defaults": {"engines": {"llama-cpp": "b10289"}}}
    cpu_only = store.Manifest(
        {**base, "engines": {"llama-cpp": {"b10289": {"linux-x64": {"path": "cpu", "sha256": "s"}}}}}
    )
    assert cpu_only.variant("engines", "llama-cpp").path == "cpu"  # falls back to cpu
    with_vulkan = store.Manifest(
        {
            **base,
            "engines": {
                "llama-cpp": {
                    "b10289": {
                        "linux-x64": {"path": "cpu", "sha256": "s"},
                        "linux-x64-vulkan": {"path": "vk", "sha256": "s"},
                    }
                }
            },
        }
    )
    assert with_vulkan.variant("engines", "llama-cpp").path == "vk"  # prefers accel when present


def test_variant_candidates_cpu_only_without_gpu(monkeypatch):
    monkeypatch.setattr(store.sys, "platform", "linux")
    monkeypatch.setattr(store.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(store, "_has_gpu", lambda: False)
    assert store.variant_candidates() == ["linux-x64"]
