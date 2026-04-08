# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Shared fixtures for CI-only tests."""

from pathlib import Path

import pytest

# Every test in this directory is automatically marked as ci.
# This allows `pytest -m "not ci"` (the local default) to skip them.


CI_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items):
    """Auto-mark every test collected under tests/ci/."""
    for item in items:
        if Path(item.fspath).is_relative_to(CI_DIR):
            item.add_marker(pytest.mark.ci)
