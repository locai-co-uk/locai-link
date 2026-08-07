# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""DEFAULT_API_URL env override: unset and empty fall back to production; a
set value wins. The constant is read at import time, so each case reloads."""

import importlib

import pytest

_PROD = "https://api.locai.co.uk/api/v1"


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        (None, _PROD),
        ("", _PROD),
        ("https://dev.api.locai.co.uk/api/v1", "https://dev.api.locai.co.uk/api/v1"),
    ],
)
def test_default_api_url_env(monkeypatch, env_value, expected):
    if env_value is None:
        monkeypatch.delenv("LOCAI_API_URL", raising=False)
    else:
        monkeypatch.setenv("LOCAI_API_URL", env_value)
    import link.constants

    reloaded = importlib.reload(link.constants)
    assert reloaded.DEFAULT_API_URL == expected


@pytest.fixture(autouse=True)
def _restore_constants():
    """Re-reload with the real environment after each case so the mutated
    module state can't leak into other tests."""
    yield
    import link.constants

    importlib.reload(link.constants)
