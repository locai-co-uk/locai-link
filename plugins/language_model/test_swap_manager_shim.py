# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Lifecycle tests for SwapManager's ownership of the CORS proxy.

These pin the contract that fixes the "port already in use; refusing to
start" / "CORS proxy is down; attempting restart" flap: when CORS is
enabled, the proxy is owned by SwapManager (one per public port), brought
up with llama-swap, kept across model reloads, and torn down BEFORE
llama-swap when the last model goes — so two servings on one port can
never race two proxies for the same socket.

A separate test pins the **zero-CORS** path: with no allowed origins,
no proxy is ever instantiated and llama-swap binds the public port
directly — the perf-preserving default.

llama-swap and the real proxy socket are mocked out so the test needs no
binaries and binds no real ports.
"""

from __future__ import annotations

import socket

import pytest

try:
    from . import swap_manager as sm_mod
    from .swap_manager import SwapManager
except ImportError:  # flat layout (pytest prepend import mode)
    import swap_manager as sm_mod  # type: ignore
    from swap_manager import SwapManager  # type: ignore


def _free_base_port() -> int:
    """A public base port whose listen port (base + offset) is also free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeProxy:
    """Records start/stop without binding a real socket."""

    def __init__(self, events: list, public_port: int, upstream_port: int, **_kw) -> None:
        self.events = events
        self.public_port = public_port
        self.upstream_port = upstream_port
        self.starts = 0
        self.stops = 0
        self._running = False
        events.append(("proxy_new", public_port, upstream_port))

    def start(self) -> None:
        self.starts += 1
        self._running = True

    def stop(self) -> None:
        self.stops += 1
        self._running = False
        self.events.append(("proxy_stop", self.public_port))

    def is_running(self) -> bool:
        return self._running


class _FakeProc:
    """Stand-in for the llama-swap subprocess — always reports alive until terminated."""

    def __init__(self, events: list) -> None:
        self.events = events
        self.returncode = None
        self._alive = True

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self) -> None:
        self._alive = False
        self.returncode = 0
        self.events.append(("swap_stop",))

    def wait(self, timeout=None):
        return self.returncode

    def kill(self) -> None:
        self._alive = False
        self.returncode = -9

    def send_signal(self, _sig) -> None:  # SIGHUP reload path (non-Windows)
        pass


def _make_manager(tmp_path, monkeypatch, *, allowed_origins: list[str] | None) -> SwapManager:
    """A SwapManager wired to fakes, on a free port, with configs in tmp_path."""
    monkeypatch.chdir(tmp_path)
    events: list = []

    # No real binary, no real sleep, deterministic reload path (stop+start).
    monkeypatch.setattr(sm_mod.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(sm_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sm_mod, "CorsProxy", lambda **kw: _FakeProxy(events, **kw))

    def _fake_popen(*_a, **_k):
        return _FakeProc(events)

    monkeypatch.setattr(sm_mod.subprocess, "Popen", _fake_popen)

    sm = SwapManager(_free_base_port(), "127.0.0.1", tmp_path, allowed_origins=allowed_origins)
    # Pretend the binary exists so _start proceeds to spawn the fake proc.
    monkeypatch.setattr(type(sm._swap_bin), "exists", lambda _self: True, raising=False)
    sm._events = events  # expose for assertions
    return sm


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """SwapManager with CORS enabled (proxy in front of llama-swap)."""
    return _make_manager(tmp_path, monkeypatch, allowed_origins=["http://localhost:3000"])


@pytest.fixture
def manager_no_cors(tmp_path, monkeypatch):
    """SwapManager with CORS disabled (zero-cost path — proxy never instantiated)."""
    return _make_manager(tmp_path, monkeypatch, allowed_origins=None)


def test_add_model_brings_up_exactly_one_proxy(manager):
    manager.add_model("m1", "/models/m1.gguf")
    proxy_news = [e for e in manager._events if e[0] == "proxy_new"]
    assert len(proxy_news) == 1, "exactly one proxy should be created"
    # Its upstream is llama-swap's loopback listen port, not the public port.
    assert proxy_news[0][2] == manager.listen_port
    assert manager._cors_proxy is not None and manager._cors_proxy.is_running()
    assert manager._cors_proxy.starts == 1


def test_reload_does_not_create_a_second_proxy(manager):
    manager.add_model("m1", "/models/m1.gguf")
    first_proxy = manager._cors_proxy
    # Second model on the same port reloads llama-swap (Windows: stop+start).
    manager.add_model("m2", "/models/m2.gguf")
    proxy_news = [e for e in manager._events if e[0] == "proxy_new"]
    assert len(proxy_news) == 1, "reload must reuse the existing proxy, not race a new one"
    assert manager._cors_proxy is first_proxy
    assert first_proxy.is_running()
    # The proxy was never stopped across the reload — public port stayed up.
    assert first_proxy.stops == 0


def test_removing_last_model_stops_proxy_before_swap(manager):
    manager.add_model("m1", "/models/m1.gguf")
    manager.remove_model("m1")
    # Proxy cleared, and stopped strictly before llama-swap.
    assert manager._cors_proxy is None
    order = [e[0] for e in manager._events if e[0] in ("proxy_stop", "swap_stop")]
    assert order == ["proxy_stop", "swap_stop"], f"proxy must stop before swap; got {order}"


def test_removing_one_of_two_models_keeps_proxy_up(manager):
    manager.add_model("m1", "/models/m1.gguf")
    manager.add_model("m2", "/models/m2.gguf")
    proxy = manager._cors_proxy
    manager.remove_model("m1")  # m2 remains
    assert manager._cors_proxy is proxy and proxy.is_running()
    assert proxy.stops == 0, "proxy must stay up while another model is still served"


def test_ensure_proxy_is_a_noop_when_swap_not_running(manager):
    # No add_model yet — proc is None, so ensure_proxy must not create a proxy.
    manager.ensure_proxy()
    assert manager._cors_proxy is None
    assert not any(e[0] == "proxy_new" for e in manager._events)


def test_ensure_proxy_is_idempotent_when_already_running(manager):
    manager.add_model("m1", "/models/m1.gguf")
    starts_before = manager._cors_proxy.starts
    manager.ensure_proxy()
    manager.ensure_proxy()
    # No new proxy, just extra (harmless) start() calls on the same instance.
    assert len([e for e in manager._events if e[0] == "proxy_new"]) == 1
    assert manager._cors_proxy.starts == starts_before + 2


# ---------------------------------------------------------------------------
# Zero-CORS path — the perf-preserving default
# ---------------------------------------------------------------------------


def test_no_cors_skips_proxy_entirely(manager_no_cors):
    """Without allowed_origins, llama-swap binds the public port directly."""
    manager_no_cors.add_model("m1", "/models/m1.gguf")
    assert manager_no_cors.cors_enabled is False
    assert manager_no_cors._cors_proxy is None
    # listen_port equals the public port — no offset, no proxy in the path.
    assert manager_no_cors.listen_port == manager_no_cors.port
    assert not any(e[0] == "proxy_new" for e in manager_no_cors._events)


def test_no_cors_remove_does_not_touch_proxy(manager_no_cors):
    """Teardown path stays clean when there was never a proxy to stop."""
    manager_no_cors.add_model("m1", "/models/m1.gguf")
    manager_no_cors.remove_model("m1")
    assert manager_no_cors._cors_proxy is None
    assert not any(e[0] == "proxy_stop" for e in manager_no_cors._events)
