# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for the language_model plugin's ModelServer lifecycle.

These tests run without the llama-server binary or a real model — they mock
`subprocess.Popen` and `requests.get` to exercise the Python control logic
that was historically Windows-fragile: async startup, health-check URL
construction, stop-during-startup cancellation, and start/stop/start cycles.

Integration tests (real binary, real model) live in the plugin directory and
run as a separate CI job.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

pytest.importorskip("link_language_model.server", reason="language_model plugin not installed")

from link_language_model.server import ModelServer  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_bin(tmp_path):
    """A path that exists so _get_server_binary passes validation."""
    p = tmp_path / "llama-server"
    p.touch()
    return p


@pytest.fixture
def make_server(fake_bin, tmp_path, monkeypatch):
    """Factory for a ModelServer wired up for mocked testing."""
    monkeypatch.chdir(tmp_path)
    created: list[ModelServer] = []

    def _make(host: str = "127.0.0.1", port: int = 19000) -> ModelServer:
        srv = ModelServer(model_path="/tmp/fake.gguf", port=port, host=host)
        monkeypatch.setattr(srv, "_get_server_binary", lambda: fake_bin)
        monkeypatch.setattr(srv, "_is_port_in_use", lambda p: False)
        created.append(srv)
        return srv

    yield _make

    for srv in created:
        srv.stop()


def _alive_proc():
    """Return a MagicMock that behaves like a live subprocess with empty stdout."""
    proc = MagicMock()
    proc.pid = 12345
    proc.poll.return_value = None  # None = still alive
    proc.stdout.readline.return_value = ""  # EOF immediately so monitor thread exits
    return proc


# ---------------------------------------------------------------------------
# start() non-blocking
# ---------------------------------------------------------------------------


def test_start_returns_quickly_even_when_health_never_succeeds(make_server):
    srv = make_server(port=19001)
    with (
        patch("subprocess.Popen", return_value=_alive_proc()),
        patch("requests.get", side_effect=requests.RequestException("never")),
    ):
        t0 = time.time()
        srv.start()
        elapsed = time.time() - t0

    assert elapsed < 1.0, f"start() blocked for {elapsed:.2f}s; must return immediately"
    assert srv.running is True
    assert srv.ready is False


# ---------------------------------------------------------------------------
# wait_until_ready() state matrix
# ---------------------------------------------------------------------------


def test_wait_until_ready_returns_true_once_health_passes(make_server):
    srv = make_server(port=19002)
    calls = {"n": 0}

    def fake_get(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.RequestException("loading")
        return MagicMock(ok=True, status_code=200)

    with patch("subprocess.Popen", return_value=_alive_proc()), patch("requests.get", side_effect=fake_get):
        srv.start()
        assert srv.wait_until_ready(timeout=10) is True

    assert srv.ready is True
    assert calls["n"] >= 3


def test_wait_until_ready_returns_false_on_timeout(make_server):
    srv = make_server(port=19003)
    with (
        patch("subprocess.Popen", return_value=_alive_proc()),
        patch("requests.get", side_effect=requests.RequestException("never")),
    ):
        srv.start()
        t0 = time.time()
        ok = srv.wait_until_ready(timeout=1.5)
        elapsed = time.time() - t0

    assert ok is False
    assert 1.0 <= elapsed < 3.0, f"timeout honored loosely; got {elapsed:.2f}s"


def test_wait_until_ready_bails_when_process_dies(make_server):
    srv = make_server(port=19004)
    proc = _alive_proc()

    with patch("subprocess.Popen", return_value=proc), patch(
        "requests.get", side_effect=requests.RequestException("loading")
    ):
        srv.start()
        # Flip the process to 'exited' after a short delay.
        threading.Timer(0.3, lambda: setattr(proc, "poll", lambda: 1)).start()
        t0 = time.time()
        ok = srv.wait_until_ready(timeout=30)
        elapsed = time.time() - t0

    assert ok is False
    # wait_until_ready polls self.running every 0.2s; should bail within ~1s of process death.
    assert elapsed < 2.0, f"dead process should unblock waiter fast; took {elapsed:.2f}s"


def test_wait_until_ready_cancelled_by_concurrent_stop(make_server):
    srv = make_server(port=19005)
    with (
        patch("subprocess.Popen", return_value=_alive_proc()),
        patch("requests.get", side_effect=requests.RequestException("never")),
    ):
        srv.start()
        # Trigger stop() from another thread while wait_until_ready is polling.
        threading.Timer(0.3, srv.stop).start()
        t0 = time.time()
        ok = srv.wait_until_ready(timeout=30)
        elapsed = time.time() - t0

    assert ok is False
    assert elapsed < 2.0, f"stop() should unblock waiter; took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# 503 != ready
# ---------------------------------------------------------------------------


def test_http_503_is_treated_as_still_loading(make_server):
    """Guards the 'llama-server responds 503 while loading' bug — only 2xx = ready."""
    srv = make_server(port=19006)
    resp_503 = MagicMock(ok=False, status_code=503)

    with patch("subprocess.Popen", return_value=_alive_proc()), patch("requests.get", return_value=resp_503):
        srv.start()
        ok = srv.wait_until_ready(timeout=1.5)

    assert ok is False, "503 must not flip ready=True"
    assert srv.ready is False


# ---------------------------------------------------------------------------
# Wildcard bind → loopback connect (Windows regression guard)
# ---------------------------------------------------------------------------


def test_health_check_remaps_wildcard_bind_to_loopback(make_server):
    """Directly guards the Windows WSAEADDRNOTAVAIL bug: connecting to 0.0.0.0 fails on Windows."""
    srv = make_server(host="0.0.0.0", port=19007)
    urls: list[str] = []

    def fake_get(url, *_a, **_kw):
        urls.append(url)
        raise requests.RequestException("n/a")

    with patch("requests.get", side_effect=fake_get):
        srv._wait_for_health(timeout=0.5)

    assert urls, "health check should have attempted at least one GET"
    assert all("127.0.0.1" in u for u in urls), f"0.0.0.0 must be remapped to loopback; got {urls}"
    assert not any("0.0.0.0" in u for u in urls)


@pytest.mark.parametrize("bind_host,expected_in_url", [
    ("127.0.0.1", "127.0.0.1"),
    ("192.168.1.50", "192.168.1.50"),
])
def test_health_check_preserves_non_wildcard_hosts(make_server, bind_host, expected_in_url):
    """Explicit hosts (loopback or LAN) must be used verbatim, not remapped."""
    srv = make_server(host=bind_host, port=19008)
    urls: list[str] = []

    with patch("requests.get", side_effect=lambda u, *a, **kw: urls.append(u) or (_ for _ in ()).throw(requests.RequestException())):  # noqa: E501
        srv._wait_for_health(timeout=0.5)

    assert urls and all(expected_in_url in u for u in urls)


# ---------------------------------------------------------------------------
# start/stop/start cycle
# ---------------------------------------------------------------------------


def test_start_stop_start_cycle_resets_state(make_server):
    """Ensure a fresh start() after stop() correctly resets ready, stop_event, and running flags."""
    srv = make_server(port=19009)
    resp_200 = MagicMock(ok=True, status_code=200)

    with patch("subprocess.Popen", return_value=_alive_proc()), patch("requests.get", return_value=resp_200):
        # Cycle 1
        srv.start()
        assert srv.running is True
        assert srv.wait_until_ready(timeout=2) is True
        assert srv.ready is True

        srv.stop()
        assert srv.running is False
        assert srv.ready is False
        assert srv._stop_event.is_set()

        # Cycle 2 — state must reset
        srv.start()
        assert srv.running is True, "running should flip back to True on restart"
        assert not srv._stop_event.is_set(), "stop_event must be cleared on restart"
        assert srv.wait_until_ready(timeout=2) is True, "ready should flip on second cycle"
        srv.stop()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_log_tail_emits_last_n_lines(make_server, caplog, tmp_path):
    srv = make_server(port=19010)
    srv.log_path = tmp_path / "server.log"
    srv.log_path.write_text("\n".join(f"line_{i}" for i in range(30)) + "\n")

    with caplog.at_level(logging.ERROR, logger="link_language_model.server"):
        srv._log_tail(lines=5)

    messages = [r.message for r in caplog.records]
    # Tail should include lines 25..29 and exclude earlier ones.
    assert any("line_29" in m for m in messages)
    assert any("line_25" in m for m in messages)
    assert not any("line_24" in m for m in messages)


def test_startup_log_shows_localhost_when_bound_to_wildcard(make_server, caplog):
    srv = make_server(host="0.0.0.0", port=19011)
    with (
        caplog.at_level(logging.INFO, logger="link_language_model.server"),
        patch("subprocess.Popen", return_value=_alive_proc()),
        patch("requests.get", side_effect=requests.RequestException("n/a")),
    ):
        srv.start()

    startup_lines = [r.message for r in caplog.records if "Starting Model Server" in r.message]
    assert startup_lines, "expected a 'Starting Model Server' log line"
    assert all("localhost" in line for line in startup_lines), f"expected localhost in: {startup_lines}"
    assert not any("0.0.0.0" in line for line in startup_lines)


def test_startup_log_preserves_explicit_host(make_server, caplog):
    srv = make_server(host="192.168.1.50", port=19012)
    with (
        caplog.at_level(logging.INFO, logger="link_language_model.server"),
        patch("subprocess.Popen", return_value=_alive_proc()),
        patch("requests.get", side_effect=requests.RequestException("n/a")),
    ):
        srv.start()

    startup_lines = [r.message for r in caplog.records if "Starting Model Server" in r.message]
    assert startup_lines
    assert all("192.168.1.50" in line for line in startup_lines)
    assert not any("localhost" in line for line in startup_lines)
