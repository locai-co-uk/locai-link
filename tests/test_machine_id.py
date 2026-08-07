# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for the per-OS machine-id reader (Step 5.1).

The control plane dedups re-enrollments by this hash, so two properties matter:
it must be a stable 64-char SHA-256 hex digest (the backend enforces min_length=8),
and the raw OS id must never leak — only the digest is returned.
"""

import hashlib
import re

import pytest

import link.infra.utils as mid


def test_hash_is_64_char_lowercase_hex(mocker):
    mocker.patch.object(mid, "_read_raw_id", return_value="stable-raw-id")
    h = mid.get_machine_id_hash()
    assert re.fullmatch(r"[0-9a-f]{64}", h)
    assert h == hashlib.sha256(b"stable-raw-id").hexdigest()


def test_hash_is_deterministic_for_same_machine(mocker):
    mocker.patch.object(mid, "_read_raw_id", return_value="same-machine")
    assert mid.get_machine_id_hash() == mid.get_machine_id_hash()


def test_raw_id_never_returned_to_caller(mocker):
    """get_machine_id_hash must hand back only the digest, not the raw id."""
    mocker.patch.object(mid, "_read_raw_id", return_value="SECRET-RAW-UUID")
    assert "SECRET-RAW-UUID" not in mid.get_machine_id_hash()


# --- per-OS dispatch ---


@pytest.mark.parametrize(
    "system,reader,expected",
    [
        ("Linux", "_read_linux", "linux-id"),
        ("Windows", "_read_windows", "win-guid"),
        ("Darwin", "_read_macos", "mac-uuid"),
    ],
)
def test_reader_dispatched_per_os(mocker, system, reader, expected):
    mocker.patch("link.infra.utils.platform.system", return_value=system)
    reader_mock = mocker.patch(f"link.infra.utils.{reader}", return_value=expected)
    assert mid._read_raw_id() == expected
    reader_mock.assert_called_once()


def test_native_read_failure_falls_back(mocker):
    """A failing native reader must not propagate — it falls back to the UUID file."""
    mocker.patch("link.infra.utils.platform.system", return_value="Linux")
    mocker.patch("link.infra.utils._read_linux", side_effect=OSError("no /etc/machine-id"))
    mocker.patch("link.infra.utils._fallback_id", return_value="fallback-uuid")
    assert mid._read_raw_id() == "fallback-uuid"


def test_unknown_os_uses_fallback(mocker):
    mocker.patch("link.infra.utils.platform.system", return_value="Plan9")
    mocker.patch("link.infra.utils._fallback_id", return_value="fallback-uuid")
    assert mid._read_raw_id() == "fallback-uuid"


# --- persisted fallback ---


def test_fallback_creates_then_reuses_file(mocker, tmp_path):
    """First call generates + persists a UUID; later calls return the same value."""
    fpath = tmp_path / ".machine_id"
    mocker.patch.object(mid, "_FALLBACK_FILE", fpath)

    first = mid._fallback_id()
    assert fpath.exists()
    assert first == fpath.read_text(encoding="utf-8").strip()

    second = mid._fallback_id()
    assert second == first  # reused, not regenerated → hash stays stable across restarts


def test_fallback_hash_stable_across_calls(mocker, tmp_path):
    """End-to-end: with no native id, the hash is stable because the UUID persists."""
    mocker.patch("link.infra.utils.platform.system", return_value="Plan9")
    mocker.patch.object(mid, "_FALLBACK_FILE", tmp_path / ".machine_id")
    assert mid.get_machine_id_hash() == mid.get_machine_id_hash()
