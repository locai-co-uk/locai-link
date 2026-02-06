# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import atexit
import os
import platform
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


class ModelServer:
    """Manages the lifecycle of the local model server using official llama.cpp binaries."""

    def __init__(self, payload: dict):
        """Initialises the ModelServer with basic auth and paths.

        payload {
            "id": command_doc["id"],
            "model_id": model_id,
            "model_name": payload["model_name"],
            "model_display_name": payload["model_display_name"],
            "port": payload["port"],
            "host": payload["host"],
            "device_id": command_data["device_id"],
            "deployed_at": command_data["created_at"],
            "status": command_data["status"],
        }
        """
        self.pid_file = PROJECT_ROOT / "serving.pid"
        self.log_file = PROJECT_ROOT / "serving.log"
        self.bin_dir = PROJECT_ROOT / "bin"  # Directory where binaries are installed

        self.is_valid = False
        self.model_path = None
        self.params = {}
        self.host = payload.get("host", "localhost")
        self.port = payload.get("port", 8003)
        self.model_id = payload.get("model_id")
        self.model_display_name = payload.get("model_display_name")
        self.model_config = None

        atexit.register(self.stop)

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
            payload = {"status": "online", "mode": "serving"}

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
                    f"Status check returned unexpected code: {response.status_code} - {response.text}",
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
        model_conf_path = CONFIGS_DIR / f"{self.model_id}.json"

        if model_conf_path.exists():
            self.model_config = load_json_config(model_conf_path)
        else:
            link_logger.fail(
                f"Model config for {self.model_id} not found.",
                category="configuration",
                action="start_server",
                hint="Run 'register' first to generate a config.",
            )
            return False

        process = self.model_config.get("process", {})
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

    def _is_port_in_use(self, port: int) -> bool:
        """Checks if a port is actively in use.

        Returns:
            bool: True if the port is in use, False otherwise.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("localhost", port)) == 0

    def _get_server_binary(self) -> Path:
        """Locates the platform-specific llama-server binary in the VENV.

        Returns:
            Path: The path to the llama-server binary.
        """
        system = platform.system()
        binary_name = "llama-server.exe" if system == "Windows" else "llama-server"

        candidate = PROJECT_ROOT / ".venv" / "bin-llama" / binary_name
        if candidate.exists():
            return candidate

        return None

    def is_running(self) -> bool:
        """Checks if the server is running.

        Returns:
            bool: True if the server is running, False otherwise.
        """
        return is_process_running(self.pid_file)

    def start(self):
        """Starts the llama-server with robust backend discovery."""
        if not self.is_valid:
            link_logger.fail("Server configuration is invalid.")
            return

        if self.is_running():
            pid = self.pid_file.read_text().strip()
            link_logger.warn(f"Server is already running (PID {pid}).")
            return

        if self._is_port_in_use(self.port):
            link_logger.fail(f"Port {self.port} is busy.")
            return

        print("Starting Model Serving...")

        if not self._load_and_parse_runtime_config():
            return

        if not self.model_path or not self.model_path.exists():
            link_logger.fail(f"Model file not found: {self.model_path}")
            return

        server_bin = self._get_server_binary()
        if not server_bin:
            link_logger.fail("Inference Engine binary not found. Run installer.")
            return

        env = os.environ.copy()
        bin_dir = server_bin.parent
        lib_paths = set()

        # Always add the binary's own directory
        lib_paths.add(str(bin_dir))

        # Search for the critical backend library 'libggml-cpu.so' (or similar)
        # This ensures we find the EXACT folder where the plugins live.
        backend_lib_found = False
        for root, dirs, files in os.walk(bin_dir):
            for file in files:
                if "ggml-cpu" in file and file.endswith(".so"):
                    lib_paths.add(root)
                    backend_lib_found = True

        if not backend_lib_found:
            print("WARNING: Could not find 'ggml-cpu' shared object. Server might fail to load backends.")

        # Construct LD_LIBRARY_PATH
        current_ld = env.get("LD_LIBRARY_PATH", "")
        new_ld_path = os.pathsep.join(list(lib_paths))
        if current_ld:
            new_ld_path += f"{os.pathsep}{current_ld}"

        env["LD_LIBRARY_PATH"] = new_ld_path

        cmd = [
            str(server_bin),
            "--model",
            str(self.model_path),
            "--alias",
            str(self.model_display_name),
            "--host",
            str(self.host),
            "--port",
            str(self.port),
        ]

        param_map = {
            "n_gpu_layers": "--n-gpu-layers",
            "n_ctx": "--ctx-size",
            "n_batch": "--batch-size",
            "n_threads": "--threads",
            "chat_format": "--chat-template",
        }

        for config_key, cli_flag in param_map.items():
            if config_key in self.params:
                val = self.params[config_key]
                if val is not None:
                    cmd.extend([cli_flag, str(val)])

        if "chat_format" not in self.params:
            cmd.extend(["--chat-template", "chatml"])

        link_logger.info(f"Launching server on http://{self.host}:{self.port}")

        try:
            with open(self.log_file, "w") as log:
                process = subprocess.Popen(
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=PROJECT_ROOT,
                    env=env,  # Critical
                )

            self.pid_file.write_text(str(process.pid))
            link_logger.ok(f"Server started (PID {process.pid})")

            time.sleep(2)
            if process.poll() is not None:
                link_logger.fail("Server crashed immediately. Check logs.")
                if self.pid_file.exists():
                    self.pid_file.unlink()
                return

        except Exception as e:
            link_logger.fail(f"Failed to start server: {e}")

        # Notify backend (fire and forget)
        url = f"{self.api_url}/agent/{self.device_id}/models/{self.model_id}/status"
        payload = {"running": False, "pid": 0, "serving": True, "serving_pid": process.pid, "serving_port": self.port}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            requests.post(url, json=payload, headers=headers, timeout=5)
        except Exception:
            pass

    def stop(self):
        """Stops the running server securely."""
        # If we have a direct handle (started in this session)
        if getattr(self, "process", None):
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

        # Always check the PID file to be sure
        if getattr(self, "pid_file", None) and self.pid_file.exists():
            stop_process_tree(self.pid_file, "Model Server")

        if (
            getattr(self, "api_url", None)
            and getattr(self, "device_id", None)
            and getattr(self, "model_id", None)
            and getattr(self, "api_key", None)
        ):
            try:
                url = f"{self.api_url}/agent/{self.device_id}/models/{self.model_id}/status"
                payload = {
                    "running": False,
                    "pid": 0,
                    "serving": False,
                    "serving_pid": 0,
                    "serving_port": 0,
                }
                headers = {"Authorization": f"Bearer {self.api_key}"}

                requests.post(url, json=payload, headers=headers, timeout=2)

            except Exception:
                pass
