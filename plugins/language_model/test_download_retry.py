# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""_download_with_retry: bounded, atomic, retries only transient failures.

urlopen and sleep are mocked, so no network and no real backoff.
"""

from __future__ import annotations

import io
import urllib.error
from email.message import Message

import pytest

try:
    from . import install
except ImportError:  # flat layout (pytest prepend import mode)
    import install


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):  # pyright: ignore[reportUnusedFunction]  (autouse: discovered by decorator)
    monkeypatch.setattr(install.time, "sleep", lambda *_: None)


class _Opener:
    """urlopen stub that yields each result in turn.

    A bytes result is served as the response body; an exception is raised.
    """

    def __init__(self, *results):
        self._results = results
        self.calls = {"n": 0}

    def __call__(self, url, timeout=None):
        i = self.calls["n"]
        self.calls["n"] += 1
        item = self._results[min(i, len(self._results) - 1)]
        if isinstance(item, Exception):
            raise item
        return io.BytesIO(item)


def _fake_urlopen(*results) -> _Opener:
    return _Opener(*results)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", Message(), None)


def test_success_writes_dest_and_cleans_partial(monkeypatch, tmp_path):
    monkeypatch.setattr(install.urllib.request, "urlopen", _fake_urlopen(b"payload"))
    dest = tmp_path / "asset.bin"
    install._download_with_retry("http://x/asset.bin", dest)
    assert dest.read_bytes() == b"payload"
    assert not dest.with_suffix(".bin.partial").exists()


def test_retries_transient_then_succeeds(monkeypatch, tmp_path):
    opener = _fake_urlopen(urllib.error.URLError("reset"), b"ok")
    monkeypatch.setattr(install.urllib.request, "urlopen", opener)
    dest = tmp_path / "asset.bin"
    install._download_with_retry("http://x/asset.bin", dest, attempts=3)
    assert dest.read_bytes() == b"ok"
    assert opener.calls["n"] == 2


def test_raises_after_exhausting_attempts(monkeypatch, tmp_path):
    opener = _fake_urlopen(urllib.error.URLError("down"))
    monkeypatch.setattr(install.urllib.request, "urlopen", opener)
    dest = tmp_path / "asset.bin"
    with pytest.raises(urllib.error.URLError):
        install._download_with_retry("http://x/asset.bin", dest, attempts=3)
    assert opener.calls["n"] == 3
    assert not dest.with_suffix(".bin.partial").exists()


def test_http_4xx_fails_fast_without_retry(monkeypatch, tmp_path):
    opener = _fake_urlopen(_http_error(404))
    monkeypatch.setattr(install.urllib.request, "urlopen", opener)
    dest = tmp_path / "asset.bin"
    with pytest.raises(urllib.error.HTTPError):
        install._download_with_retry("http://x/asset.bin", dest, attempts=3)
    assert opener.calls["n"] == 1  # not retried


def test_http_5xx_is_retried(monkeypatch, tmp_path):
    opener = _fake_urlopen(_http_error(503), b"recovered")
    monkeypatch.setattr(install.urllib.request, "urlopen", opener)
    dest = tmp_path / "asset.bin"
    install._download_with_retry("http://x/asset.bin", dest, attempts=3)
    assert dest.read_bytes() == b"recovered"
    assert opener.calls["n"] == 2
