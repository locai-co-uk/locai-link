# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Zenoh router process management — spawn, monitor, session factory."""

import json
import logging
import platform
import time
from pathlib import Path
from typing import Any

from link.adapters.zenoh_client import ZenohClient
from link.config.models import TransportConfig
from link.infra.provision import ZenohProvisioner
from link.infra.service import ServiceManager

logger = logging.getLogger(__name__)


class ZenohRouter:
    """Manages the lifecycle of the Zenoh Router binary (zenohd)."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        config_path: Path | None = None,
        zenoh_dir: Path = Path.cwd() / ".zenoh",
        working_dir: Path = Path.cwd(),
    ):
        """Initialises the ZenohRouter manager.

        Args:
            config (dict[str, Any] | None): Dictionary configuration (preferred).
            config_path (Path | None): Legacy path to a static file (fallback).
            zenoh_dir (Path): Directory for binaries/plugins (hidden).
            working_dir (Path): Root for database/logs (Defaults to current dir).
        """
        self.zenoh_dir = zenoh_dir
        self.working_dir = working_dir

        self.zenoh_dir.mkdir(parents=True, exist_ok=True)

        # 1. Config Generation
        if config:
            # We write the generated config to .zenoh so it doesn't clutter root
            self.config_path = self.zenoh_dir / "generated_router.json5"
            self._write_config_file(config, self.config_path)
        elif config_path:
            self.config_path = config_path
        else:
            self.config_path = Path.cwd() / "configs/zenoh_router.json5"

        self.binary_name = "zenohd.exe" if platform.system() == "Windows" else "zenohd"
        self.binary_path = self.zenoh_dir / self.binary_name

        # 2. Environment Variables
        # We explicitly tell RocksDB that the "Root" is the working dir.
        # This ensures 'link_db' is created in CWD, not inside .zenoh
        backend = "rocksdb"
        if config and "storage" in config:
            backend = config["storage"].get("type", "rocksdb")

        root_key = "ZENOH_BACKEND_ROCKSDB_ROOT" if backend == "rocksdb" else "ZENOH_BACKEND_FS_ROOT"

        self.env_vars = {
            "RUST_LOG": "info",
            root_key: str(self.working_dir),
        }

    def _write_config_file(self, config: dict[str, Any], path: Path):
        """Generates the router config file.

        Args:
            config (dict[str, Any]): The configuration dictionary.
            path (Path): The output path.
        """
        endpoints = config.get("endpoints", ["tcp/0.0.0.0:7447"])

        router_conf = {"mode": "router", "listen": {"endpoints": endpoints}, "plugins": {}}

        if "storage" in config:
            s_conf = config["storage"]
            backend = s_conf.get("type", "rocksdb")
            db_dir = s_conf.get("dir", "link_db")
            key_expr = s_conf.get("key_expr", "locai/devices/**")

            router_conf["plugins"]["storage_manager"] = {
                "volumes": {backend: {}},
                "storages": {
                    "main_storage": {
                        "key_expr": key_expr,
                        "strip_prefix": key_expr.replace("/**", ""),
                        "volume": {"id": backend, "dir": db_dir, "create_db": True},
                    }
                },
            }

        with open(path, "w") as f:
            json.dump(router_conf, f, indent=4)

    def is_installed(self):
        return self.binary_path.exists()

    def is_running(self):
        return ServiceManager("zenohd").is_running()

    def _get_command(self):
        return f'"{self.binary_path}" -c "{self.config_path}"'

    def install_service(self, start_now=True):
        if not self.is_installed():
            logger.error(f"Router binary not found at {self.binary_path}")
            return

        if ServiceManager("zenohd").is_installed():
            logger.debug("Zenoh router service already installed.")
            if start_now and not self.is_running():
                ServiceManager("zenohd").start()
        else:
            logger.info(f"Installing Zenoh router service (Root: {self.working_dir})...")
            manager = ServiceManager(
                service_name="zenohd",
                command=self._get_command(),
                description="Loc.ai Network Router",
                # FIX: Run in Project Root so logs/db appear there
                working_dir=self.working_dir,
                env_vars=self.env_vars,
            )
            manager.install(start_now=start_now)

    def stop_service(self):
        try:
            ServiceManager("zenohd").stop()
        except Exception:
            pass

    def uninstall_service(self):
        try:
            ServiceManager("zenohd").uninstall()
        except Exception:
            pass


def get_or_create_zenoh_session(config: TransportConfig) -> Any:
    """Factory: Manages Infra and returns a Session.

    Args:
        config (TransportConfig): The transport configuration.

    Returns:
        zenoh.Session: The active Zenoh session.

    Raises:
        RuntimeError: If connection fails after retries.
    """
    raw_args = config.args
    mode = raw_args.get("mode", "client")

    # 1. Provision + start a LOCAL router only when this device is hosting one.
    # In pure client mode the router lives elsewhere (e.g. central GCE VM) and
    # we just dial it — no local zenohd binary, no service install, no .zenoh dir.
    if mode in ("router", "peer"):
        if not ZenohProvisioner.is_router_installed():
            ZenohProvisioner.install_router_env()
        if mode == "router":
            _ensure_router_running(raw_args)

    # 3. Connect Client
    client_args = raw_args.copy()

    if mode == "router":
        client_args["mode"] = "client"

        new_endpoints = []
        for ep in client_args.get("endpoints", []):
            new_endpoints.append(ep.replace("0.0.0.0", "127.0.0.1"))
        client_args["endpoints"] = new_endpoints

    logger.debug(f"Connecting to Zenoh ({client_args['endpoints']})...")
    client = ZenohClient(args=client_args)

    for i in range(5):
        try:
            return client.get_session()
        except Exception as e:
            if i == 4:
                raise e
            time.sleep(1)


def _ensure_router_running(args: dict):
    """Ensures the Zenoh Router is running, installing if necessary.

    Args:
        args (dict): Configuration for the router.

    Raises:
        RuntimeError: If router fails to start.
    """
    router = ZenohRouter(config=args)
    if not router.is_running():
        router.install_service(start_now=True)
        # Wait for spin up
        for _ in range(20):
            if router.is_running():
                return
            time.sleep(0.5)
        raise RuntimeError("Zenoh Router failed to start.")
