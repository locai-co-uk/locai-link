# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for the provisioner's download + safe-extract integration.

The extractor itself is covered by test_archive.py; here we verify the
provisioner delegates to it and cleans up the downloaded archive on both
success and a rejected (unsafe/unknown) archive.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from link.infra.provision import ZenohProvisioner
from link.utils.archive import UnknownArchiveType, UnsafeArchiveEntry


def _fake_urlopen(data: bytes = b"archive-bytes") -> MagicMock:
    """A urlopen(...) context manager whose response.read() yields `data`."""
    resp = MagicMock()
    resp.read.return_value = data
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


def test_download_component_extracts_and_cleans_up(tmp_path):
    url = "https://example.test/comp.tar.gz"
    with (
        patch("link.infra.provision.urllib.request.urlopen", return_value=_fake_urlopen()) as urlopen,
        patch("link.infra.provision.extract_archive") as extract,
    ):
        ZenohProvisioner._download_component("Comp", url, tmp_path)

    urlopen.assert_called_once()
    extract.assert_called_once_with(tmp_path / "comp.tar.gz", tmp_path)
    # Archive removed after a successful extraction.
    assert not (tmp_path / "comp.tar.gz").exists()


@pytest.mark.parametrize("exc", [UnsafeArchiveEntry("bad entry"), UnknownArchiveType("bad type")])
def test_download_component_rejects_and_cleans_up(tmp_path, exc):
    url = "https://example.test/comp.tar.gz"
    with (
        patch("link.infra.provision.urllib.request.urlopen", return_value=_fake_urlopen()),
        patch("link.infra.provision.extract_archive", side_effect=exc),
    ):
        # A rejected archive is logged and swallowed, never propagated.
        ZenohProvisioner._download_component("Comp", url, tmp_path)

    # The downloaded archive is cleaned up rather than left on disk.
    assert not (tmp_path / "comp.tar.gz").exists()
