# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import pytest

from link.infra.utils import get_platform_arch


def test_windows(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr("platform.machine", lambda: "AMD64")

    assert get_platform_arch() == "x86_64-pc-windows-msvc"


def test_mac_intel(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")

    assert get_platform_arch() == "x86_64-apple-darwin"


def test_mac_silicon(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")

    assert get_platform_arch() == "aarch64-apple-darwin"


def test_ubuntu_debian_standard(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr("platform.libc_ver", lambda: ("glibc", "2.35"))
    monkeypatch.setattr("platform.release", lambda: "5.15.0-generic")

    assert get_platform_arch() == "x86_64-unknown-linux-gnu"


def test_linux_aarch64_gnu(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "aarch64")
    monkeypatch.setattr("platform.libc_ver", lambda: ("glibc", "2.31"))
    monkeypatch.setattr("platform.release", lambda: "5.10.0-v8+")

    assert get_platform_arch() == "aarch64-unknown-linux-gnu"


def test_linux_alpine_musl(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr("platform.libc_ver", lambda: ("", ""))
    monkeypatch.setattr("platform.release", lambda: "5.4.0-alpine")

    assert get_platform_arch() == "x86_64-unknown-linux-musl"


def test_unsupported_arch(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "mips64")

    with pytest.raises(RuntimeError):
        get_platform_arch()


def test_unsupported_os(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "JavaOS")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")

    with pytest.raises(RuntimeError):
        get_platform_arch()
