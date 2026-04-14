# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import atexit
import socket
import subprocess
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


class BaseServer:
    """Shared lifecycle management for local inference server processes."""

    def __init__(self, payload: dict):
        """Initialises the server with connection credentials from payload."""
        self.pid_file = PROJECT_ROOT / "serving.pid"
        self.log_file = PROJECT_ROOT / "serving.log"
        self.bin_dir = PROJECT_ROOT / "bin"

        self.is_valid = False
        self.model_path = None
        self.params = {}
        self.host = payload.get("host", "localhost")
        self.port = payload.get("port", 8003)
        self.model_id = payload.get("model_id")
        self.model_display_name = payload.get("model_display_name")
        self.model_config = None

        self.process = None
        self.log_handle = None
        self.running = False

        atexit.register(self.stop)

        self.base_conf = load_json_config(AGENT_CONFIG_PATH)
        if not self.base_conf:
            link_logger.fail("Base config not found.")
            return

        self.device_id = self.base_conf.get("device_id")
        self.api_key = self.base_conf.get("api_key")
        self.api_url = self.base_conf.get("api_url")

        if self.device_id and self.api_key and self.api_url:
            LogClient.get().configure(self.device_id, self.api_key, self.api_url)
        else:
            link_logger.fail("Incomplete configuration.")
            return

        if not self._verify_connection_and_status():
            return

        self.is_valid = True

    def _verify_connection_and_status(self) -> bool:
        """Sends a status update to verify security credentials."""
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            payload = {"status": "online", "mode": "serving"}
            url = f"{self.api_url}/agent/{self.device_id}/status"
            response = requests.put(url, json=payload, headers=headers, timeout=10)
            return response.status_code == 200
        except Exception:
            return False

    def _load_and_parse_runtime_config(self) -> bool:
        """Loads the heavy device configuration."""
        model_conf_path = CONFIGS_DIR / f"{self.model_id}.json"

        if model_conf_path.exists():
            self.model_config = load_json_config(model_conf_path)
        else:
            link_logger.fail(f"Model config for {self.model_id} not found.")
            return False

        process = self.model_config.get("process", {})
        artifacts = process.get("artifacts", [])

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

        if not self.model_path:
            gguf_files = list(MODELS_DIR.glob("*.gguf"))
            if gguf_files:
                self.model_path = gguf_files[0]
                link_logger.info(f"Auto-selected model: {self.model_path.name}")

        self.params = process.get("parameters", {})
        return True

    def _is_port_in_use(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("localhost", port)) == 0

    def _get_server_binary(self):
        """Return the path to the server binary. Subclasses must implement."""
        raise NotImplementedError

    def is_running(self) -> bool:
        """Checks if the server is running."""
        return is_process_running(self.pid_file)

    def start(self):
        """Start the server. Subclasses must implement."""
        raise NotImplementedError

    def _wait_for_ready(self, timeout_seconds: int = 120) -> bool:
        url = f"http://{self.host}:{self.port}/health"
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                resp = requests.get(url, timeout=2)
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def stop(self):
        """Stops the running server securely."""
        self.running = False
        if getattr(self, "process", None):
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

        if getattr(self, "log_handle", None):
            try:
                self.log_handle.close()
            except Exception:
                pass

        if getattr(self, "pid_file", None) and self.pid_file.exists():
            stop_process_tree(self.pid_file, "Model Server")

        self._send_status(False, False)

    def _send_status(self, running, serving):
        if hasattr(self, "api_url"):
            try:
                url = f"{self.api_url}/agent/{self.device_id}/models/{self.model_id}/status"
                payload = {
                    "running": running,
                    "pid": 0,
                    "serving": serving,
                    "serving_pid": self.process.pid if self.process else 0,
                    "serving_port": self.port if serving else 0,
                }
                headers = {"Authorization": f"Bearer {self.api_key}"}
                requests.post(url, json=payload, headers=headers, timeout=2)
            except Exception:
                pass
