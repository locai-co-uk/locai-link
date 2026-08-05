# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Binary provisioning: downloads pinned Zenoh and plugin artefacts."""

import logging
import os
import platform
import stat
import urllib.request
from pathlib import Path

from link.utils.archive import extract_archive

from .utils import get_platform_arch

logger = logging.getLogger(__name__)

ZENOH_VERSION = "1.9.0"
ZENOH_DIR = Path.cwd() / ".zenoh"

BASE_URL_RELEASE = f"https://github.com/eclipse-zenoh/zenoh/releases/download/{ZENOH_VERSION}"
BASE_URL_ROCKSDB = f"https://github.com/eclipse-zenoh/zenoh-backend-rocksdb/releases/download/{ZENOH_VERSION}"
BASE_URL_FILESYSTEM = f"https://github.com/eclipse-zenoh/zenoh-backend-filesystem/releases/download/{ZENOH_VERSION}"


class ZenohProvisioner:
    """Manages the acquisition of external binaries (Zenoh Router & Plugins)."""

    @staticmethod
    def is_router_installed() -> bool:
        """Return True if the router binary is installed."""
        binary_name = "zenohd.exe" if platform.system() == "Windows" else "zenohd"
        return (ZENOH_DIR / binary_name).exists()

    @staticmethod
    def install_router_env(backend: str = "rocksdb"):
        """Download the Zenoh router and a storage backend ("rocksdb" or "filesystem")."""
        logger.info(f"Provisioning Zenoh Infrastructure ({ZENOH_VERSION})...")
        logger.info(f"Selected Storage Backend: {backend}")

        ZENOH_DIR.mkdir(parents=True, exist_ok=True)

        try:
            target_os = get_platform_arch()
            logger.info(f"Detected Platform Target: {target_os}")
        except RuntimeError as e:
            logger.error(str(e))
            return

        # 1. Download Router
        ZenohProvisioner._download_component(
            name="Zenoh Router",
            url=f"{BASE_URL_RELEASE}/zenoh-{ZENOH_VERSION}-{target_os}-standalone.zip",
            target_dir=ZENOH_DIR,
        )

        # 2. Make Executable (Linux/Mac)
        zenohd_name = "zenohd.exe" if platform.system() == "Windows" else "zenohd"
        zenohd_path = ZENOH_DIR / zenohd_name
        if zenohd_path.exists() and platform.system() != "Windows":
            st = os.stat(zenohd_path)
            os.chmod(zenohd_path, st.st_mode | stat.S_IEXEC)

        # 3. Download Backend
        if backend == "rocksdb":
            url = f"{BASE_URL_ROCKSDB}/zenoh-backend-rocksdb-{ZENOH_VERSION}-{target_os}-standalone.zip"
            ZenohProvisioner._download_component("RocksDB Backend", url, ZENOH_DIR)
        elif backend == "filesystem":
            url = f"{BASE_URL_FILESYSTEM}/zenoh-backend-filesystem-{ZENOH_VERSION}-{target_os}-standalone.zip"
            ZenohProvisioner._download_component("Filesystem Backend", url, ZENOH_DIR)
        else:
            logger.warning(f"Unknown backend '{backend}'. Skipping plugin download.")

    @staticmethod
    def _download_component(name: str, url: str, target_dir: Path):
        """Download and extract a component archive into ``target_dir``."""
        filename = Path(url).name
        file_path = target_dir / filename

        if file_path.exists():
            return

        logger.info(f"Downloading {name}...")
        try:
            # Add User-Agent to satisfy GitHub protections
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req) as response, open(file_path, "wb") as out_file:
                out_file.write(response.read())

            logger.info(f"Extracting {filename}...")
            extract_archive(file_path, target_dir)

            os.remove(file_path)
        except Exception as e:
            logger.error(f"Failed to download/extract {name}: {e}")
            if file_path.exists():
                os.remove(file_path)
