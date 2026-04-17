# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import logging
import os
import platform
import stat
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from .utils import get_platform_arch

logger = logging.getLogger(__name__)

# Constants
ZENOH_VERSION = "1.7.2"
ZENOH_DIR = Path.cwd() / ".zenoh"

BASE_URL_RELEASE = f"https://github.com/eclipse-zenoh/zenoh/releases/download/{ZENOH_VERSION}"
BASE_URL_ROCKSDB = f"https://github.com/eclipse-zenoh/zenoh-backend-rocksdb/releases/download/{ZENOH_VERSION}"
BASE_URL_FILESYSTEM = f"https://github.com/eclipse-zenoh/zenoh-backend-filesystem/releases/download/{ZENOH_VERSION}"


class ZenohProvisioner:
    """Manages the acquisition of external binaries (Zenoh Router & Plugins)."""

    @staticmethod
    def is_router_installed() -> bool:
        """Checks if the Router binary exists.

        Returns:
            bool: True if installed, False otherwise.
        """
        binary_name = "zenohd.exe" if platform.system() == "Windows" else "zenohd"
        return (ZENOH_DIR / binary_name).exists()

    @staticmethod
    def install_router_env(backend: str = "rocksdb"):
        """Downloads Zenoh Router and specified storage backend.

        Args:
            backend (str): "rocksdb" (default) or "filesystem".
        """
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
        """Downloads and extracts a component archive.

        Args:
            name (str): Display name of the component.
            url (str): The download URL.
            target_dir (Path): Directory to extract to.
        """
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
            if filename.endswith(".zip"):
                with zipfile.ZipFile(file_path, "r") as zip_ref:
                    zip_ref.extractall(target_dir)
            elif filename.endswith("gz") or filename.endswith("tgz"):
                with tarfile.open(file_path, "r:gz") as tar_ref:
                    tar_ref.extractall(target_dir)

            os.remove(file_path)
        except Exception as e:
            logger.error(f"Failed to download/extract {name}: {e}")
            if file_path.exists():
                os.remove(file_path)
