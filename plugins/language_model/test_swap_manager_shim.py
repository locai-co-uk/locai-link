# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Lifecycle tests for SwapManager's ownership of the CORS shim.

These pin the contract that fixes the "port already in use; refusing to
start" / "CORS shim is down; attempting restart" flap: the shim is owned by
SwapManager (one per public port), brought up with llama-swap, kept across
model reloads, and torn down BEFORE llama-swap when the last model goes — so
two servings on one port can never race two shims for the same socket.

llama-swap and the real shim socket are mocked out so the test needs no
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


class _FakeShim:
    """Records start/stop without binding a real socket."""

    def __init__(self, events: list, public_port: int, upstream_port: int, host: str = "0.0.0.0") -> None:
        self.events = events
        self.public_port = public_port
        self.upstream_port = upstream_port
        self.host = host
        self.starts = 0
        self.stops = 0
        self._running = False
        events.append(("shim_new", public_port, upstream_port))

    def start(self) -> None:
        self.starts += 1
        self._running = True

    def stop(self) -> None:
        self.stops += 1
        self._running = False
        self.events.append(("shim_stop", self.public_port))

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


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """A SwapManager wired to fakes, on a free port, with configs in tmp_path."""
    monkeypatch.chdir(tmp_path)
    events: list = []

    # No real binary, no real sleep, deterministic reload path (stop+start).
    monkeypatch.setattr(sm_mod.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(sm_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sm_mod, "CorsShim", lambda **kw: _FakeShim(events, **kw))

    def _fake_popen(*_a, **_k):
        return _FakeProc(events)

    monkeypatch.setattr(sm_mod.subprocess, "Popen", _fake_popen)

    sm = SwapManager(_free_base_port(), "127.0.0.1", tmp_path)
    # Pretend the binary exists so _start proceeds to spawn the fake proc.
    monkeypatch.setattr(type(sm._swap_bin), "exists", lambda _self: True, raising=False)
    sm._events = events  # expose for assertions
    return sm


def test_add_model_brings_up_exactly_one_shim(manager):
    manager.add_model("m1", "/models/m1.gguf")
    shim_news = [e for e in manager._events if e[0] == "shim_new"]
    assert len(shim_news) == 1, "exactly one shim should be created"
    # Its upstream is llama-swap's loopback listen port, not the public port.
    assert shim_news[0][2] == manager.listen_port
    assert manager._cors_shim is not None and manager._cors_shim.is_running()
    assert manager._cors_shim.starts == 1


def test_reload_does_not_create_a_second_shim(manager):
    manager.add_model("m1", "/models/m1.gguf")
    first_shim = manager._cors_shim
    # Second model on the same port reloads llama-swap (Windows: stop+start).
    manager.add_model("m2", "/models/m2.gguf")
    shim_news = [e for e in manager._events if e[0] == "shim_new"]
    assert len(shim_news) == 1, "reload must reuse the existing shim, not race a new one"
    assert manager._cors_shim is first_shim
    assert first_shim.is_running()
    # The shim was never stopped across the reload — public port stayed up.
    assert first_shim.stops == 0


def test_removing_last_model_stops_shim_before_swap(manager):
    manager.add_model("m1", "/models/m1.gguf")
    manager.remove_model("m1")
    # Shim cleared, and stopped strictly before llama-swap.
    assert manager._cors_shim is None
    order = [e[0] for e in manager._events if e[0] in ("shim_stop", "swap_stop")]
    assert order == ["shim_stop", "swap_stop"], f"shim must stop before swap; got {order}"


def test_removing_one_of_two_models_keeps_shim_up(manager):
    manager.add_model("m1", "/models/m1.gguf")
    manager.add_model("m2", "/models/m2.gguf")
    shim = manager._cors_shim
    manager.remove_model("m1")  # m2 remains
    assert manager._cors_shim is shim and shim.is_running()
    assert shim.stops == 0, "shim must stay up while another model is still served"


def test_ensure_shim_is_a_noop_when_swap_not_running(manager):
    # No add_model yet — proc is None, so ensure_shim must not create a shim.
    manager.ensure_shim()
    assert manager._cors_shim is None
    assert not any(e[0] == "shim_new" for e in manager._events)


def test_ensure_shim_is_idempotent_when_already_running(manager):
    manager.add_model("m1", "/models/m1.gguf")
    starts_before = manager._cors_shim.starts
    manager.ensure_shim()
    manager.ensure_shim()
    # No new shim, just extra (harmless) start() calls on the same instance.
    assert len([e for e in manager._events if e[0] == "shim_new"]) == 1
    assert manager._cors_shim.starts == starts_before + 2
