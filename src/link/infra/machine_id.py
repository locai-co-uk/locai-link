# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Per-OS stable machine identifier, hashed before sending to the control plane.

Privacy model: the raw OS machine-id is NEVER sent over the wire.  Only its
SHA-256 hex-digest is transmitted and stored, so the control plane can
recognise the same physical machine (for cap-dedup and re-enroll idempotency)
without holding a value that could fingerprint the device in other contexts.

Resolution order:
1. Platform-native id:
   - Linux   : /etc/machine-id
   - Windows  : HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid (registry)
   - macOS   : IOPlatformUUID via ioreg
2. Persistent fallback UUID written to <CWD>/configs/.machine_id — covers
   containers, network-booted nodes, and unusual setups that lack a native id.
   The file is created on first call and reused on subsequent calls so the
   hash remains stable for the lifetime of the installation.
"""

import hashlib
import logging
import platform
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Fallback file lives next to the session state files so a `reset --hard`
# clears it together with the rest of the local identity.
_FALLBACK_FILE = Path("configs") / ".machine_id"


def get_machine_id_hash() -> str:
    """Return the SHA-256 hex-digest of the local machine identifier.

    The raw identifier is never returned or logged — only the digest.
    Guaranteed to return a 64-character hex string.

    Returns:
        str: 64-character lowercase hex SHA-256 digest.
    """
    raw = _read_raw_id()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Internal readers
# ---------------------------------------------------------------------------


def _read_raw_id() -> str:
    """Try platform-native readers; fall back to a persisted UUID on failure."""
    system = platform.system()
    try:
        if system == "Linux":
            return _read_linux()
        elif system == "Windows":
            return _read_windows()
        elif system == "Darwin":
            return _read_macos()
    except Exception as exc:
        logger.debug("Native machine-id read failed (%s: %s) — using fallback.", system, exc)
    return _fallback_id()


def _read_linux() -> str:
    """Read /etc/machine-id (systemd standard, present on all modern distros)."""
    return Path("/etc/machine-id").read_text(encoding="utf-8").strip()


def _read_windows() -> str:
    """Read MachineGuid from the Windows registry (stable across user accounts)."""
    import winreg  # stdlib on Windows; guarded by the platform check in _read_raw_id

    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
    try:
        value, _ = winreg.QueryValueEx(key, "MachineGuid")
        return str(value).strip()
    finally:
        winreg.CloseKey(key)


def _read_macos() -> str:
    """Read IOPlatformUUID via ioreg (tied to hardware; survives OS reinstalls)."""
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
    """Return (or create) a stable UUID persisted to <CWD>/configs/.machine_id.

    Called when the platform-native reader is unavailable or fails.  The file
    is created atomically on first call; subsequent calls just re-read it.

    Returns:
        str: A UUID4 string, stable across process restarts.
    """
    import uuid

    _FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)

    if _FALLBACK_FILE.exists():
        stored = _FALLBACK_FILE.read_text(encoding="utf-8").strip()
        if stored:
            return stored

    new_id = str(uuid.uuid4())
    try:
        _FALLBACK_FILE.write_text(new_id, encoding="utf-8")
        logger.info("Generated persistent fallback machine-id (no native OS id available).")
    except Exception as exc:
        logger.warning("Could not persist fallback machine-id (%s) — id is session-scoped.", exc)
    return new_id
