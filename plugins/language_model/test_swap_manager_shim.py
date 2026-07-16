# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Lifecycle tests for SwapManager's ownership of ServingProxy.

Pins the contract: one proxy per public port, brought up with llama-swap,
kept across model reloads, and torn down BEFORE llama-swap when the last
model goes — so two servings on one port never race for the socket. The
proxy is universal (also the telemetry capture point), so the no-CORS
fixture still gets one; only ACAO emission is gated on ``allowed_origins``.

llama-swap and the proxy socket are mocked, so no binaries and no real ports.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import Any

import pytest

try:
    from . import swap_manager as sm_mod
    from .swap_manager import SwapManager
except ImportError:  # flat layout (pytest prepend import mode)
    import swap_manager as sm_mod
    from swap_manager import SwapManager


def _free_base_port() -> int:
    """A public base port whose listen port (base + offset) is also free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeProxy:
    """Records start/stop without binding a real socket."""

    def __init__(self, events: list[Any], public_port: int, upstream_port: int, **_kw) -> None:
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

    # Deterministic PIDs across the test suite so pidfile assertions are stable.
    _next_pid = 90000

    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.returncode = None
        self._alive = True
        _FakeProc._next_pid += 1
        self.pid = _FakeProc._next_pid

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


class _TestSwapManager(SwapManager):
    """SwapManager subclass carrying a test-only event log for assertions."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._events: list[Any] = []


def _make_manager(tmp_path, monkeypatch, *, allowed_origins: list[str] | None) -> _TestSwapManager:
    """A SwapManager wired to fakes, on a free port, with configs in tmp_path."""
    monkeypatch.chdir(tmp_path)
    events: list[Any] = []

    # No real binary, no real sleep, deterministic reload path (stop+start).
    monkeypatch.setattr(sm_mod.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(sm_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sm_mod, "ServingProxy", lambda **kw: _FakeProxy(events, **kw))

    def _fake_popen(*_a, **_k):
        return _FakeProc(events)

    monkeypatch.setattr(sm_mod.subprocess, "Popen", _fake_popen)

    sm = _TestSwapManager(_free_base_port(), "127.0.0.1", tmp_path, allowed_origins=allowed_origins)
    # Create the binary path so _start's existence check passes. Real file
    # rather than a monkeypatch on Path.exists — patching the class globally
    # makes every other Path.exists() in the process return True too, which
    # silently breaks anything that checks for file removal (pidfile cleanup,
    # config cleanup, etc.).
    sm._swap_bin.parent.mkdir(parents=True, exist_ok=True)
    sm._swap_bin.touch()
    sm._events = events  # expose for assertions
    return sm


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """SwapManager with CORS enabled (proxy in front of llama-swap)."""
    return _make_manager(tmp_path, monkeypatch, allowed_origins=["http://localhost:3000"])


@pytest.fixture
def manager_no_cors(tmp_path, monkeypatch):
    """SwapManager with CORS off. Proxy still runs (for telemetry); just no ACAO."""
    return _make_manager(tmp_path, monkeypatch, allowed_origins=None)


def test_add_model_brings_up_exactly_one_proxy(manager):
    manager.add_model("m1", "/models/m1.gguf")
    proxy_news = [e for e in manager._events if e[0] == "proxy_new"]
    assert len(proxy_news) == 1, "exactly one proxy should be created"
    # Its upstream is llama-swap's loopback listen port, not the public port.
    assert proxy_news[0][2] == manager.listen_port
    assert manager._proxy is not None and manager._proxy.is_running()
    assert manager._proxy.starts == 1


def test_reload_does_not_create_a_second_proxy(manager):
    manager.add_model("m1", "/models/m1.gguf")
    first_proxy = manager._proxy
    # Second model on the same port reloads llama-swap (Windows: stop+start).
    manager.add_model("m2", "/models/m2.gguf")
    proxy_news = [e for e in manager._events if e[0] == "proxy_new"]
    assert len(proxy_news) == 1, "reload must reuse the existing proxy, not race a new one"
    assert manager._proxy is first_proxy
    assert first_proxy.is_running()
    # The proxy was never stopped across the reload — public port stayed up.
    assert first_proxy.stops == 0


def test_removing_last_model_stops_proxy_before_swap(manager):
    manager.add_model("m1", "/models/m1.gguf")
    manager.remove_model("m1")
    # Proxy cleared, and stopped strictly before llama-swap.
    assert manager._proxy is None
    order = [e[0] for e in manager._events if e[0] in ("proxy_stop", "swap_stop")]
    assert order == ["proxy_stop", "swap_stop"], f"proxy must stop before swap; got {order}"


def test_removing_one_of_two_models_keeps_proxy_up(manager):
    manager.add_model("m1", "/models/m1.gguf")
    manager.add_model("m2", "/models/m2.gguf")
    proxy = manager._proxy
    manager.remove_model("m1")  # m2 remains
    assert manager._proxy is proxy and proxy.is_running()
    assert proxy.stops == 0, "proxy must stay up while another model is still served"


def test_ensure_proxy_is_a_noop_when_swap_not_running(manager):
    # No add_model yet — proc is None, so ensure_proxy must not create a proxy.
    manager.ensure_proxy()
    assert manager._proxy is None
    assert not any(e[0] == "proxy_new" for e in manager._events)


def test_ensure_proxy_is_idempotent_when_already_running(manager):
    manager.add_model("m1", "/models/m1.gguf")
    starts_before = manager._proxy.starts
    manager.ensure_proxy()
    manager.ensure_proxy()
    # No new proxy, just extra (harmless) start() calls on the same instance.
    assert len([e for e in manager._events if e[0] == "proxy_new"]) == 1
    assert manager._proxy.starts == starts_before + 2


# ---------------------------------------------------------------------------
# No-CORS path — proxy still runs (for telemetry); just no ACAO emitted
# ---------------------------------------------------------------------------


def test_no_cors_still_instantiates_proxy(manager_no_cors):
    """Universal proxy: even with no allowed_origins, the proxy is in path."""
    manager_no_cors.add_model("m1", "/models/m1.gguf")
    # cors_enabled reflects the ACAO behavior, not whether a proxy exists.
    assert manager_no_cors.cors_enabled is False
    assert manager_no_cors._proxy is not None
    assert manager_no_cors._proxy.is_running()
    # listen_port is the proxy's upstream — always offset, regardless of CORS.
    assert manager_no_cors.listen_port == manager_no_cors.port + manager_no_cors._PROXY_OFFSET
    proxy_news = [e for e in manager_no_cors._events if e[0] == "proxy_new"]
    assert len(proxy_news) == 1


def test_no_cors_remove_still_tears_down_proxy(manager_no_cors):
    """Last-model teardown stops the proxy before llama-swap, CORS or no CORS."""
    manager_no_cors.add_model("m1", "/models/m1.gguf")
    manager_no_cors.remove_model("m1")
    assert manager_no_cors._proxy is None
    order = [e[0] for e in manager_no_cors._events if e[0] in ("proxy_stop", "swap_stop")]
    assert order == ["proxy_stop", "swap_stop"]


# ---------------------------------------------------------------------------
# Pidfile + orphan reclaim
# ---------------------------------------------------------------------------
#
# These tests pin the contract that Link survives an unclean shutdown:
# the next _start() must identify *its own* orphan via the pidfile,
# terminate it cleanly, and refuse to touch foreign processes.


class _FakePsutilProcess:
    """Stand-in for psutil.Process — controllable name/cmdline and termination."""

    def __init__(self, events: list[Any], pid: int, name: str, cmdline: list[str], alive: bool = True) -> None:
        self.events = events
        self.pid = pid
        self._name = name
        self._cmdline = cmdline
        self._alive = alive

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        return list(self._cmdline)

    def terminate(self) -> None:
        self.events.append(("reclaim_terminate", self.pid))
        self._alive = False

    def kill(self) -> None:
        self.events.append(("reclaim_kill", self.pid))
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def is_alive(self) -> bool:
        return self._alive


def _install_fake_psutil(monkeypatch, events, *, alive_processes: dict[int, _FakePsutilProcess]):
    """Patch sm_mod.psutil so Process(pid) hits our table.

    Any PID not in ``alive_processes`` raises NoSuchProcess — which is exactly
    what real psutil does for a dead PID.
    """

    class _NoSuchProcess(Exception):
        pass

    class _AccessDenied(Exception):
        pass

    class _TimeoutExpired(Exception):
        pass

    class _PsutilError(Exception):
        pass

    def _process(pid: int):
        if pid not in alive_processes:
            raise _NoSuchProcess(pid)
        return alive_processes[pid]

    fake = SimpleNamespace(
        Process=_process,
        NoSuchProcess=_NoSuchProcess,
        AccessDenied=_AccessDenied,
        TimeoutExpired=_TimeoutExpired,
        Error=_PsutilError,
    )
    monkeypatch.setattr(sm_mod, "psutil", fake)
    return fake


def test_pidfile_written_on_start(manager):
    manager.add_model("m1", "/models/m1.gguf")
    pidfile = manager._pid_path
    assert pidfile.exists(), "pidfile should be created when llama-swap starts"
    assert int(pidfile.read_text()) == manager._proc.pid


def test_pidfile_cleared_on_stop(manager):
    manager.add_model("m1", "/models/m1.gguf")
    pidfile = manager._pid_path
    assert pidfile.exists()
    manager.remove_model("m1")
    assert not pidfile.exists(), "pidfile should be removed when llama-swap stops cleanly"


def test_reclaim_kills_orphan_with_matching_cmdline(tmp_path, monkeypatch):
    """Live PID whose cmdline matches → terminate cleanly, then proceed with start."""
    sm = _make_manager(tmp_path, monkeypatch, allowed_origins=None)
    sm._pid_path.parent.mkdir(parents=True, exist_ok=True)
    sm._pid_path.write_text("4242")

    # Cmdline must include either THIS manager's resolved config path OR its
    # listen address — that's what _looks_like_our_llama_swap matches against
    # (a relative or unrelated config path is treated as a different instance,
    # not as ours). Mirror what _start actually passes to Popen.
    orphan = _FakePsutilProcess(
        sm._events,
        pid=4242,
        name="llama-swap",
        cmdline=[
            "/usr/local/bin/llama-swap",
            "--config",
            str(sm._config_path.resolve()),
            "--listen",
            f"127.0.0.1:{sm._listen_port}",
        ],
    )
    _install_fake_psutil(monkeypatch, sm._events, alive_processes={4242: orphan})
    # After reclaim_previous_instance terminates the orphan, the port is free.
    monkeypatch.setattr(SwapManager, "_port_in_use", lambda _self: False)

    sm.add_model("m1", "/models/m1.gguf")
    assert ("reclaim_terminate", 4242) in sm._events
    assert sm._pid_path.exists()  # new pid written
    assert sm._proc is not None
    assert int(sm._pid_path.read_text()) == sm._proc.pid


def test_reclaim_dead_pid_just_clears_pidfile(tmp_path, monkeypatch):
    """Pidfile points at a PID nothing's using anymore → clean up, proceed."""
    sm = _make_manager(tmp_path, monkeypatch, allowed_origins=None)
    sm._pid_path.parent.mkdir(parents=True, exist_ok=True)
    sm._pid_path.write_text("9999")
    _install_fake_psutil(monkeypatch, sm._events, alive_processes={})  # PID dead
    monkeypatch.setattr(SwapManager, "_port_in_use", lambda _self: False)

    sm.add_model("m1", "/models/m1.gguf")
    # No reclaim_terminate event — there was nothing to terminate.
    assert not any(e[0] == "reclaim_terminate" for e in sm._events)
    # But the new run wrote its own pidfile.
    assert sm._pid_path.exists()
    assert sm._proc is not None
    assert int(sm._pid_path.read_text()) == sm._proc.pid


def test_reclaim_skips_unrelated_process(tmp_path, monkeypatch):
    """PID reused by something that isn't llama-swap → don't kill, clear pidfile, refuse port-conflict."""
    sm = _make_manager(tmp_path, monkeypatch, allowed_origins=None)
    sm._pid_path.parent.mkdir(parents=True, exist_ok=True)
    sm._pid_path.write_text("4242")

    unrelated = _FakePsutilProcess(
        sm._events,
        pid=4242,
        name="postgres",
        cmdline=["/usr/local/bin/postgres", "-D", "/var/lib/postgresql/data"],
    )
    _install_fake_psutil(monkeypatch, sm._events, alive_processes={4242: unrelated})
    # Port held by the unrelated process — should refuse rather than kill.
    monkeypatch.setattr(SwapManager, "_port_in_use", lambda _self: True)

    with pytest.raises(RuntimeError) as exc:
        sm.add_model("m1", "/models/m1.gguf")
    assert "we don't own" in str(exc.value)
    # Crucially, the unrelated process was NOT terminated.
    assert not any(e[0] in ("reclaim_terminate", "reclaim_kill") for e in sm._events)
    # Stale pidfile was cleaned up so the next attempt starts fresh.
    assert not sm._pid_path.exists()


def test_foreign_port_holder_raises_without_pidfile(tmp_path, monkeypatch):
    """No pidfile + port held by someone else → raise with diagnostic message."""
    sm = _make_manager(tmp_path, monkeypatch, allowed_origins=None)
    _install_fake_psutil(monkeypatch, sm._events, alive_processes={})
    monkeypatch.setattr(SwapManager, "_port_in_use", lambda _self: True)

    with pytest.raises(RuntimeError) as exc:
        sm.add_model("m1", "/models/m1.gguf")
    msg = str(exc.value)
    assert "we don't own" in msg
    assert str(sm._listen_port) in msg  # diagnostic mentions the offending port


# ---------------------------------------------------------------------------
# Telemetry callback wiring + multi-model fanout
# ---------------------------------------------------------------------------


def test_registered_callback_receives_fanned_out_record(tmp_path, monkeypatch):
    """The fanout the proxy receives invokes every registered callback."""
    sm = _make_manager(tmp_path, monkeypatch, allowed_origins=None)
    received: list[dict[str, Any]] = []
    sm.add_telemetry_callback(received.append)
    sm.add_model("m1", "/models/m1.gguf")

    # The proxy is constructed with sm._fanout_telemetry; invoking it
    # synthesises what would happen when a real inference completes.
    sm._fanout_telemetry({"model": "m1", "tokens_generated": 7})

    assert received == [{"model": "m1", "tokens_generated": 7}]


def test_multi_model_fanout_lets_each_adapter_filter(tmp_path, monkeypatch):
    """Two adapters on one port — fanout reaches both; each filters by model."""
    sm = _make_manager(tmp_path, monkeypatch, allowed_origins=None)

    # Simulate two adapters' callbacks: each one keeps only its own model's records.
    a_records: list[dict[str, Any]] = []
    b_records: list[dict[str, Any]] = []

    def adapter_a(record: dict[str, Any]) -> None:
        if record.get("model") == "model-a":
            a_records.append(record)

    def adapter_b(record: dict[str, Any]) -> None:
        if record.get("model") == "model-b":
            b_records.append(record)

    sm.add_telemetry_callback(adapter_a)
    sm.add_telemetry_callback(adapter_b)
    sm.add_model("model-a", "/models/a.gguf")

    sm._fanout_telemetry({"model": "model-a", "tokens_generated": 10})
    sm._fanout_telemetry({"model": "model-b", "tokens_generated": 20})

    assert a_records == [{"model": "model-a", "tokens_generated": 10}]
    assert b_records == [{"model": "model-b", "tokens_generated": 20}]


def test_add_telemetry_callback_is_idempotent(tmp_path, monkeypatch):
    """Re-registering the same callback doesn't double-fire."""
    sm = _make_manager(tmp_path, monkeypatch, allowed_origins=None)
    received: list[dict[str, Any]] = []
    sm.add_telemetry_callback(received.append)
    sm.add_telemetry_callback(received.append)  # duplicate
    sm._fanout_telemetry({"model": "m1"})
    assert len(received) == 1
