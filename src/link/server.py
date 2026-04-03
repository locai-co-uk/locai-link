# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import atexit
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from link import logger as link_logger
from link.analytics import send_model_ready
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
        """Initialises the ModelServer."""
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
        self.telemetry_thread = None
        self.running = False

        atexit.register(self.stop)

        print("Initialising Model Server Manager...")

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

    def _get_server_binary(self) -> Path:
        system = platform.system()
        binary_name = "llama-server.exe" if system == "Windows" else "llama-server"

        candidate = PROJECT_ROOT / ".venv" / "bin-llama" / binary_name
        if candidate.exists():
            return candidate

        candidate = PROJECT_ROOT / "bin" / binary_name
        if candidate.exists():
            return candidate
        return None

    def is_running(self) -> bool:
        """Checks if the server is running."""
        return is_process_running(self.pid_file)

    def start(self):
        """Starts the llama-server."""
        if not self.is_valid or self.is_running() or self._is_port_in_use(self.port):
            return

        print("Starting Model Serving...")
        if not self._load_and_parse_runtime_config():
            return

        if not self.model_path or not self.model_path.exists():
            link_logger.fail(f"Model file not found: {self.model_path}")
            return

        server_bin = self._get_server_binary()
        if not server_bin:
            link_logger.fail("Binary not found.")
            return

        # Setup Env
        env = os.environ.copy()
        bin_dir = server_bin.parent
        lib_paths = {str(bin_dir)}
        for root, _, files in os.walk(bin_dir):
            for file in files:
                if "ggml" in file and file.endswith(".so"):
                    lib_paths.add(root)

        curr_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = os.pathsep.join(list(lib_paths)) + (f"{os.pathsep}{curr_ld}" if curr_ld else "")

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
            # "--verbose",
        ]

        param_map = {
            "n_gpu_layers": "--n-gpu-layers",
            "n_ctx": "--ctx-size",
            "n_batch": "--batch-size",
            "n_threads": "--threads",
            "chat_format": "--chat-template",
        }

        for k, v in param_map.items():
            if k in self.params:
                cmd.extend([v, str(self.params[k])])


        link_logger.info(f"Launching server on http://{self.host}:{self.port}")

        try:
            # Open with buffering=1 (line buffered)
            self.log_handle = open(self.log_file, "w", buffering=1)

            self.process = subprocess.Popen(
                cmd, stdout=self.log_handle, stderr=subprocess.STDOUT, cwd=PROJECT_ROOT, env=env, text=True
            )
            self.running = True

            self.pid_file.write_text(str(self.process.pid))
            link_logger.ok(f"Server started (PID {self.process.pid})")

            # Start Telemetry Sidecar
            self.telemetry_thread = threading.Thread(target=self._telemetry_monitor_loop, daemon=True)
            self.telemetry_thread.start()

            time.sleep(2)
            if self.process.poll() is not None:
                link_logger.fail("Server crashed immediately. Check logs.")
                self.stop()
                return

        except Exception as e:
            link_logger.fail(f"Failed to start: {e}")
            self.stop()
            return

        try:
            ready = self._wait_for_ready(timeout_seconds=120)
            if ready:
                send_model_ready(
                    base_url=self.api_url,
                    device_id=self.device_id,
                    api_key=self.api_key,
                    model_id=self.model_id or Path(self.model_path).stem,
                    model_name=Path(self.model_path).name if self.model_path else None,
                    mode="serve",
                    runner="llama-server",
                    model_format="gguf",
                )
        except Exception:
            pass

        self._send_status(False, True)

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

    def _telemetry_monitor_loop(self):
        """Monitors log file with error handling for non-utf8 characters."""
        time.sleep(1)
        if not self.log_file.exists():
            return

        try:
            with open(self.log_file, "r", encoding="utf-8", errors="replace") as f:
                while self.running:
                    line = f.readline()
                    if not line:
                        time.sleep(0.1)
                        continue

                    # Log Format: "eval time = 2587.42 ms / 2038 tokens"
                    if "eval time =" in line and "prompt" not in line:
                        self._parse_and_send_telemetry(line)

        except Exception as e:
            print(f"DEBUG: Monitor error: {e}", file=sys.stderr)

    def _parse_and_send_telemetry(self, log_line):
        try:
            # 1. Parse Duration
            dur_match = re.search(r"=\s+(\d+\.\d+)\s+ms", log_line)
            duration_ms = float(dur_match.group(1)) if dur_match else 0.0

            # 2. Parse Tokens
            token_match = re.search(r"/\s+(\d+)\s+tokens", log_line)
            tokens = int(token_match.group(1)) if token_match else 0

            # 3. Payload Construction
            now = datetime.now()
            start_time = now - timedelta(milliseconds=duration_ms)

            metadata = {
                "start_time": start_time.isoformat(),
                "end_time": now.isoformat(),
                "duration": duration_ms / 1000.0,
                "tokens_generated": tokens,
                "temperature": float(self.params.get("temperature", 0.0)),
            }

            payload = {
                "model_id": self.model_id,
                "model_type": "generation",
                "sub_model_type": "text_generation",
                "model_output_type": "telemetry",
                "model_output": "stats_only",
                "model_output_confidence": 1.0,
                "model_output_start_time": metadata["start_time"],
                "model_output_end_time": metadata["end_time"],
                "model_output_duration": metadata["duration"],
                "model_output_metadata": metadata,
            }

            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            url = f"{self.api_url}/agent/model_results/{self.device_id}/create_from_agent"

            requests.post(url, json=payload, headers=headers, timeout=10)

        except Exception as e:
            print(f"Failed to send telemetry: {e}")

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
