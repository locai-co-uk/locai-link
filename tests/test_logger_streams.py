# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""setup_logging stdio reconfiguration: both streams get the UTF-8 wrapper, and
streams that can't be reconfigured (closed, detached, exotic) never prevent
logging from coming up."""

import logging
import sys

import pytest

from link.utils.logger import setup_logging


@pytest.fixture(autouse=True)
def _restore_loggers():
    """setup_logging mutates the root and reporter loggers; undo after each case."""
    loggers = (logging.getLogger(), logging.getLogger("link.reporter"))
    saved = [(lg, list(lg.handlers), lg.level, lg.propagate) for lg in loggers]
    yield
    for lg, handlers, level, propagate in saved:
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate


class _FailingStream:
    """Minimal stdio double whose reconfigure always raises."""

    def __init__(self, exc: type[Exception]) -> None:
        self._exc = exc

    def reconfigure(self, **kwargs) -> None:
        raise self._exc("cannot reconfigure")

    def write(self, s: str) -> int:
        return len(s)

    def flush(self) -> None:
        pass


@pytest.mark.parametrize("exc", [OSError, ValueError])
def test_setup_logging_survives_reconfigure_failure(monkeypatch, exc):
    monkeypatch.setattr(sys, "stdout", _FailingStream(exc))
    monkeypatch.setattr(sys, "stderr", _FailingStream(exc))

    root = setup_logging()

    assert isinstance(root, logging.Logger)


def test_setup_logging_reconfigures_both_streams(monkeypatch):
    calls: list[dict] = []

    class _Recording:
        def reconfigure(self, **kwargs) -> None:
            calls.append(kwargs)

        def write(self, s: str) -> int:
            return len(s)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(sys, "stdout", _Recording())
    monkeypatch.setattr(sys, "stderr", _Recording())

    setup_logging()

    assert calls == [{"encoding": "utf-8", "errors": "backslashreplace"}] * 2
