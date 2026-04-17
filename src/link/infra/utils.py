# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import logging
import platform

logger = logging.getLogger(__name__)


def get_platform_arch() -> str:
    """Determines the platform architecture for the current system.

    Returns:
        str: The platform string (e.g., 'x86_64-unknown-linux-gnu').

    Raises:
        RuntimeError: If the platform is unsupported.
    """
    system = platform.system().lower()
    machine = platform.machine().lower()

    # --- 1. Windows ---
    if system == "windows":
        # Zenoh provides MSVC (standard) and GNU (MinGW). We default to MSVC.
        return "x86_64-pc-windows-msvc"

    # --- 2. macOS (Darwin) ---
    elif system == "darwin":
        if machine == "x86_64":
            return "x86_64-apple-darwin"
        elif machine == "arm64":
            # Apple Silicon (M1/M2/M3) is "aarch64" in Rust/Zenoh conventions
            return "aarch64-apple-darwin"

    # --- 3. Linux ---
    elif system == "linux":
        # Check for libc type (glibc vs musl) to support Alpine Linux
        # platform.libc_ver() usually returns ('glibc', '2.31') or ('', '') on musl
        libc_name, _ = platform.libc_ver()

        # Heuristic: If libc_name is empty or 'musl' is in platform string, assume musl
        is_musl = "musl" in libc_name.lower() or "alpine" in platform.release().lower() or not libc_name

        env_abi = "musl" if is_musl else "gnu"

        # Architecture Mapping
        if machine == "x86_64":
            return f"x86_64-unknown-linux-{env_abi}"

        elif machine == "aarch64":
            # 64-bit ARM (Raspberry Pi 4/5 64-bit, AWS Graviton)
            return f"aarch64-unknown-linux-{env_abi}"

        elif machine.startswith("armv7") or machine == "arm":
            # 32-bit ARM (Raspberry Pi 3/4 32-bit)
            # Standard for Raspbian/Debian on 32-bit Pi is 'gnueabihf' (Hard Float)
            # Zenoh binaries often use 'gnueabihf' for armv7
            return f"armv7-unknown-linux-{env_abi}eabihf"

        elif machine == "armv6l":
            # Raspberry Pi Zero / 1
            return f"arm-unknown-linux-{env_abi}eabihf"

    raise RuntimeError(f"Unsupported platform: System={system}, Machine={machine}")
