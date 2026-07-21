# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for the shared safe archive extractor (used by updater + provisioner)."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from link.utils.archive import UnknownArchiveType, UnsafeArchiveEntry, extract_archive


def _make_tar(path: Path, entries: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _make_zip(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def test_extract_tar_clean(tmp_path):
    archive = tmp_path / "ok.tar.gz"
    _make_tar(archive, {"a/b.txt": b"hi"})
    extract_archive(archive, tmp_path / "out")
    assert (tmp_path / "out" / "a" / "b.txt").read_bytes() == b"hi"


def test_extract_zip_clean(tmp_path):
    archive = tmp_path / "ok.zip"
    _make_zip(archive, {"a/b.txt": b"hi"})
    extract_archive(archive, tmp_path / "out")
    assert (tmp_path / "out" / "a" / "b.txt").read_bytes() == b"hi"


def test_extract_tar_refuses_traversal(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    _make_tar(archive, {"../escape.txt": b"oops"})
    with pytest.raises(UnsafeArchiveEntry):
        extract_archive(archive, tmp_path / "out")


def test_extract_tar_refuses_absolute(tmp_path):
    archive = tmp_path / "abs.tar.gz"
    _make_tar(archive, {"/etc/passwd": b"oops"})
    with pytest.raises(UnsafeArchiveEntry):
        extract_archive(archive, tmp_path / "out")


def test_extract_zip_refuses_traversal(tmp_path):
    archive = tmp_path / "evil.zip"
    _make_zip(archive, {"../escape.txt": b"oops"})
    with pytest.raises(UnsafeArchiveEntry):
        extract_archive(archive, tmp_path / "out")


def test_extract_unknown_type_refused(tmp_path):
    archive = tmp_path / "mystery.rar"
    archive.write_bytes(b"not an archive")
    with pytest.raises(UnknownArchiveType):
        extract_archive(archive, tmp_path / "out")
