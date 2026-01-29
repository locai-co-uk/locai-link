# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import subprocess
import sys
import time
from pathlib import Path

import requests

from link import logger as link_logger
from link.logger import LogClient
from link.utils import (
    AGENT_CONFIG_PATH,
    CONFIGS_DIR,
    MODELS_DIR,
    PROJECT_ROOT,
    is_process_running,
    load_json_config,
    stop_process_tree,
)


class ModelServer:
    """Manages the lifecycle of the local model server."""

    def __init__(self):
        """Initialises the ModelServer with basic auth and paths."""
        self.pid_file = PROJECT_ROOT / "serving.pid"
        self.log_file = PROJECT_ROOT / "serving.log"

        self.is_valid = False
        self.model_path = None
        self.params = {}
        self.device_config = None
        self.host = "[IP_ADDRESS]"
        self.port = 8003

        print("Initialising Model Server Manager...")

        self.base_conf = load_json_config(AGENT_CONFIG_PATH)
        if not self.base_conf:
            link_logger.fail(
                "Base config not found.",
                category="process",
                action="init_server",
                hint="Run 'register' first to generate a config.",
            )
            return

        self.device_id = self.base_conf.get("device_id")
        self.api_key = self.base_conf.get("api_key")
        self.api_url = self.base_conf.get("api_url")

        if self.device_id and self.api_key and self.api_url:
            LogClient.get().configure(self.device_id, self.api_key, self.api_url)
        else:
            link_logger.fail(
                "Incomplete configuration (missing ID, Key, or URL).",
                category="configuration",
                action="init_server",
            )
            return

        if not self._verify_connection_and_status():
            return

        self.is_valid = True

    def _verify_connection_and_status(self) -> bool:
        """Sends a status update to verify security credentials and connectivity.

        Returns:
            bool: True if connection is successful and authorized, False otherwise.
        """
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            payload = {"status": "starting_server"}

            url = f"{self.api_url}/agent/{self.device_id}/status"

            response = requests.put(url, json=payload, headers=headers, timeout=10)

            if response.status_code == 200:
                print("✔ Security check passed: Device authorized.")
                return True
            elif response.status_code in (401, 403):
                link_logger.fail(
                    "Security check failed: Unauthorized.",
                    category="authentication",
                    action="init_server",
                    state_after={"status_code": response.status_code},
                )
                return False
            else:
                link_logger.warn(
                    f"Status check returned unexpected code: {response.status_code}",
                    category="network",
                )
                return True
        except Exception as e:
            link_logger.fail(
                f"Failed to connect to platform: {e}",
                category="network",
                action="init_server",
            )
            return False

    def _load_and_parse_runtime_config(self) -> bool:
        """Loads the heavy device configuration and parses parameters.

        Returns:
            bool: True if config loaded successfully, False otherwise.
        """
        device_conf_path = CONFIGS_DIR / f"{self.device_id}.json"

        if device_conf_path.exists():
            self.device_config = load_json_config(device_conf_path)
        else:
            link_logger.fail(
                f"Device config for {self.device_id} not found.",
                category="configuration",
                action="start_server",
                hint="Run 'register' first to generate a config.",
            )
            return False

        # Parse State
        serving = self.device_config.get("serving", {})
        self.host = serving.get("default_host", "[IP_ADDRESS]")
        self.port = serving.get("default_port", 8003)

        process = self.device_config.get("process", {})
        artifacts = process.get("artifacts", [])

        # Try to find a GGUF model in artifacts
        for art in artifacts:
            if art.get("name") == "model" or art.get("framework") == "GGUF":
                raw_path = art.get("path")
                if raw_path:
                    filename = Path(raw_path).name
                    local_candidate = MODELS_DIR / filename
                    if local_candidate.exists():
                        self.model_path = local_candidate
                    else:
                        self.model_path = Path(raw_path)
                break

        # Fallback: Auto-discovery
        if not self.model_path:
            gguf_files = list(MODELS_DIR.glob("*.gguf"))
            if gguf_files:
                self.model_path = gguf_files[0]
                link_logger.info(f"Auto-selected model: {self.model_path.name}")

        self.params = process.get("parameters", {})
        return True

    def is_running(self) -> bool:
        """Checks if the server is running."""
        return is_process_running(self.pid_file)

    def start(self):
        """Starts the llama-server."""
        # Gatekeeper Check
        if not self.is_valid:
            print("❌ Server configuration is invalid. Aborting startup.")
            return

        if self.is_running():
            pid = self.pid_file.read_text().strip()
            print(f"Server is already running (PID {pid}). Skipping startup.")
            return

        print("Starting Model Serving...")

        # Runtime Config Check
        if not self._load_and_parse_runtime_config():
            return

        if not self.model_path or not self.model_path.exists():
            link_logger.fail(
                f"Model file not found: {self.model_path}",
                category="process",
                action="start_server",
            )
            return

        # Build Command
        cmd = [
            sys.executable,
            "-m",
            "llama_cpp.server",
            "--model",
            str(self.model_path),
            "--host",
            str(self.host),
            "--port",
            str(self.port),
        ]

        # Map JSON parameters to CLI flags
        param_map = {
            "n_gpu_layers": "--n_gpu_layers",
            "n_ctx": "--n_ctx",
            "n_batch": "--n_batch",
            "n_threads": "--n_threads",
            "chat_format": "--chat_format",
            "clip_model_path": "--clip_model_path",
        }

        for config_key, cli_flag in param_map.items():
            if config_key in self.params:
                val = self.params[config_key]
                if val is not None:
                    cmd.extend([cli_flag, str(val)])

        print(f"Launching server on http://{self.host}:{self.port}...")
        print(f"Logs: {self.log_file}")

        try:
            with open(self.log_file, "w") as log:
                process = subprocess.Popen(
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    cwd=PROJECT_ROOT,
                )

            self.pid_file.write_text(str(process.pid))
            link_logger.ok(f"Server started (PID {process.pid})")

            time.sleep(2)
            if process.poll() is not None:
                link_logger.fail(
                    "Server crashed immediately.",
                    category="process",
                    action="start_server",
                    state_after={"exit_code": process.returncode},
                    hint=f"Check {self.log_file} for details.",
                )
                self.pid_file.unlink()

        except Exception as e:
            link_logger.fail(f"Failed to start server: {e}")

    def stop(self):
        """Stops the running server."""
        print("Stopping Model Serving...")
        stop_process_tree(self.pid_file, "Model Server")
