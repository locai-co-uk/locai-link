# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Infrastructure utilities: platform/arch detection and machine identity."""

import hashlib
import logging
import platform
import subprocess
from pathlib import Path

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


# ---------------------------------------------------------------------------
# Machine identity
# Privacy: the raw OS machine-id is never sent over the wire. Only its
# SHA-256 hex-digest is transmitted so the control plane can recognise the
# same physical machine without holding a value that could fingerprint it.
#
# Resolution order:
#   1. Linux   : /etc/machine-id
#   2. Windows : HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid
#   3. macOS   : IOPlatformUUID via ioreg
#   4. Fallback: persistent UUID in <CWD>/configs/.machine_id
# ---------------------------------------------------------------------------

# Fallback file lives next to session state so a `reset --hard` clears it.
_FALLBACK_FILE = Path("configs") / ".machine_id"


def get_machine_id_hash() -> str:
    """Return the SHA-256 hex-digest of the local machine identifier.

    The raw identifier is never returned or logged. Guaranteed to return a
    64-character hex string.
    """
    raw = _read_raw_id()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_raw_id() -> str:
    """Try platform-native readers; fall back to a persisted UUID on failure."""
    system = platform.system()
    try:
        if system == "Linux":
            raw = _read_linux()
        elif system == "Windows":
            raw = _read_windows()
        elif system == "Darwin":
            raw = _read_macos()
        else:
            logger.warning(
                "Link could not identify the OS ('%s'); falling back to a persistent UUID identifier.",
                system,
            )
            return _fallback_id()
        raw = raw.strip()
        if not raw:
            # An empty /etc/machine-id would hash to the same digest on every
            # such host, breaking enrolment dedup. Treat as a read failure.
            raise RuntimeError(f"native machine-id reader for {system} returned an empty string")
        return raw
    except Exception as exc:
        logger.warning(
            "Link failed to read the native machine-id on %s (%s); falling back to a persistent UUID identifier.",
            system,
            exc,
        )
    return _fallback_id()


def _read_linux() -> str:
    return Path("/etc/machine-id").read_text(encoding="utf-8").strip()


def _read_windows() -> str:
    import winreg  # stdlib on Windows; guarded by the platform check in _read_raw_id

    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
    try:
        value, _ = winreg.QueryValueEx(key, "MachineGuid")
        return str(value).strip()
    finally:
        winreg.CloseKey(key)


def _read_macos() -> str:
    result = subprocess.run(
        ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    for line in result.stdout.splitlines():
        if "IOPlatformUUID" in line:
            # Line format:  | "IOPlatformUUID" = "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX"
            parts = line.split('"')
            if len(parts) >= 4:
                uuid_val = parts[-2].strip()
                if uuid_val:
                    return uuid_val
    raise RuntimeError("IOPlatformUUID not found in ioreg output")


def _fallback_id() -> str:
    """Return (or create) a stable UUID persisted to <CWD>/configs/.machine_id."""
    import uuid

    _FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)

    if _FALLBACK_FILE.exists():
        stored = _FALLBACK_FILE.read_text(encoding="utf-8").strip()
        if stored:
            return stored

    new_id = str(uuid.uuid4())
    try:
        _FALLBACK_FILE.write_text(new_id, encoding="utf-8")
        logger.warning("Generated persistent fallback machine-id; no native OS identifier was available.")
    except Exception as exc:
        logger.warning("Could not persist fallback machine-id (%s); id is session-scoped.", exc)
    return new_id
