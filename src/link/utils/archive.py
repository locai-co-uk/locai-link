# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Safe archive extraction shared by the updater and the provisioner.

Refuses path-traversal, absolute, and link-escape entries before extracting a
tar (.tar.gz/.tgz) or zip archive. On Python 3.12+ tar extraction also applies
the ``data`` filter defensively.
"""

from __future__ import annotations

import re
import tarfile
import zipfile
from pathlib import Path


class UnsafeArchiveEntry(Exception):
    """An archive entry would escape the destination (absolute, .., or link)."""


class UnknownArchiveType(Exception):
    """Archive is neither a recognised tar (.tar.gz/.tgz) nor a zip."""


# Rejects: root-absolute (/ or \), Windows drive-letter roots (C:\, C:/), and
# any parent-traversal (..) segment, in both slash styles.
_UNSAFE_PATH_RE = re.compile(r"(^[/\\])|(^[A-Za-z]:)|((^|[/\\])\.\.([/\\]|$))")


def _refuse_unsafe(name: str) -> None:
    if _UNSAFE_PATH_RE.search(name):
        raise UnsafeArchiveEntry(f"Refusing unsafe archive entry: {name!r}")


def extract_archive(archive: Path, dest: Path) -> None:
    """Extract a tar.gz/tgz or zip ``archive`` into ``dest``, refusing unsafe entries.

    Raises UnsafeArchiveEntry for a traversal/absolute/link-escape entry, and
    UnknownArchiveType for an unrecognised extension.
    """
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        _extract_tar(archive, dest)
    elif name.endswith(".zip"):
        _extract_zip(archive, dest)
    else:
        raise UnknownArchiveType(f"Unknown archive type: {archive.name}")


def _extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, mode="r:*") as tf:
        for member in tf.getmembers():
            _refuse_unsafe(member.name)
            if member.islnk() or member.issym():
                _refuse_unsafe(member.linkname)
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")  # type: ignore[arg-type]
        else:  # pragma: no cover -- Python <3.12, not a target version
            tf.extractall(dest)


def _extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for entry in zf.namelist():
            _refuse_unsafe(entry)
        zf.extractall(dest)
