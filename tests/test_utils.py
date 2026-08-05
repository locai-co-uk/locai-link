# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import pytest

from link.infra.utils import get_platform_arch


@pytest.mark.parametrize(
    "system,machine,libc,release,expected",
    [
        ("Windows", "AMD64", None, None, "x86_64-pc-windows-msvc"),
        ("Darwin", "x86_64", None, None, "x86_64-apple-darwin"),
        ("Darwin", "arm64", None, None, "aarch64-apple-darwin"),
        ("Linux", "x86_64", ("glibc", "2.35"), "5.15.0-generic", "x86_64-unknown-linux-gnu"),
        ("Linux", "aarch64", ("glibc", "2.31"), "5.10.0-v8+", "aarch64-unknown-linux-gnu"),
        ("Linux", "x86_64", ("", ""), "5.4.0-alpine", "x86_64-unknown-linux-musl"),
    ],
)
def test_get_platform_arch_supported(monkeypatch, system, machine, libc, release, expected):
    monkeypatch.setattr("platform.system", lambda: system)
    monkeypatch.setattr("platform.machine", lambda: machine)
    if libc is not None:
        monkeypatch.setattr("platform.libc_ver", lambda: libc)
    if release is not None:
        monkeypatch.setattr("platform.release", lambda: release)
    assert get_platform_arch() == expected


@pytest.mark.parametrize("system,machine", [("Linux", "mips64"), ("JavaOS", "x86_64")])
def test_get_platform_arch_unsupported_raises(monkeypatch, system, machine):
    monkeypatch.setattr("platform.system", lambda: system)
    monkeypatch.setattr("platform.machine", lambda: machine)
    with pytest.raises(RuntimeError):
        get_platform_arch()
