# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


class WhisperServer:
    """Manages the whisper-server background process."""

    def __init__(self, model_path, host="0.0.0.0", port=8003, **kwargs):
        self.model_path = Path(model_path)
        self.host = host
        self.port = int(port)
        self.process = None
        self.running = False

        self.language = kwargs.get("language")
        self.n_threads = kwargs.get("n_threads")
        self.beam_size = kwargs.get("beam_size")

        # Determine binary directory
        if sys.prefix != sys.base_prefix:
            self.bin_dir = Path(sys.prefix) / "bin-whisper"
        else:
            self.bin_dir = Path(__file__).parent / "bin-whisper"
            if not self.bin_dir.exists():
                self.bin_dir = Path(__file__).parent.parent / "bin-whisper"

    @staticmethod
    def build_telemetry_payload(model_id, output_text, start_time, end_time, duration, metadata):
        """Standardizes the telemetry payload structure.

        Args:
            model_id (str): The model identifier.
            output_text (str): The transcribed text.
            start_time (datetime): Start timestamp.
            end_time (datetime): End timestamp.
            duration (float): Duration in seconds.
            metadata (dict): Additional stats.
        """
        return {
            "model_id": model_id,
            "model_type": "generation",
            "sub_model_type": "audio_transcription",
            "model_output_type": "text",
            "model_output": output_text,
            "model_output_confidence": 1.0,
            "model_output_start_time": start_time.isoformat(),
            "model_output_end_time": end_time.isoformat(),
            "model_output_duration": round(duration, 2),
            "model_output_metadata": metadata,
        }

    def _get_server_binary(self):
        """Locates the platform-specific binary."""
        system = platform.system()
        binary_name = "whisper-server.exe" if system == "Windows" else "whisper-server"

        # 1. Check bin-whisper
        candidate = self.bin_dir / binary_name
        if candidate.exists():
            return candidate

        # 2. Check PATH
        path_bin = shutil.which(binary_name)
        if path_bin:
            return Path(path_bin)

        return None

    def start(self):
        """Starts the server process."""
        if self.running:
            return

        if self._is_port_in_use(self.port):
            logger.error(f"Port {self.port} is already in use!")
            return

        server_bin = self._get_server_binary()
        if not server_bin or not server_bin.exists():
            logger.error(f"whisper-server binary not found in {self.bin_dir}. Run install.py")
            return

        logger.info(f"Starting Whisper Server on http://{self.host}:{self.port}...")

        logs_dir = Path.cwd() / "logs"
        logs_dir.mkdir(exist_ok=True)
        self.log_path = logs_dir / f"whisper_{self.port}.log"

        # Prepare Environment
        env = os.environ.copy()
        if platform.system() == "Linux":
            bin_dir = server_bin.parent
            lib_paths = {str(bin_dir)}
            # Walk subdirectories to find whisper*.so files (shared libs)
            for root, _, files in os.walk(bin_dir):
                for file in files:
                    if "whisper" in file and file.endswith(".so"):
                        lib_paths.add(root)
            current_ld = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = os.pathsep.join(list(lib_paths)) + (
                f"{os.pathsep}{current_ld}" if current_ld else ""
            )

        cmd = [
            str(server_bin),
            "--model",
            str(self.model_path),
            "--host",
            str(self.host),
            "--port",
            str(self.port),
        ]

        # Optional parameters
        param_map = {
            "language": "--language",
            "n_threads": "--threads",
            "beam_size": "--beam-size",
        }
        params = {"language": self.language, "n_threads": self.n_threads, "beam_size": self.beam_size}
        for k, flag in param_map.items():
            if params.get(k) is not None:
                cmd.extend([flag, str(params[k])])

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
                bufsize=1,
            )
            self.running = True

            # Start background thread to read logs
            self.monitor_thread = threading.Thread(target=self._log_monitor_loop, daemon=True)
            self.monitor_thread.start()

            logger.info(f"Server logs: {self.log_path}")

            if not self._wait_for_health(timeout=120):
                logger.error("Whisper server failed to respond to health check.")
                self.stop()
            else:
                logger.info("Whisper server is ready.")

        except Exception as e:
            logger.error(f"Failed to launch whisper server: {e}")
            self.stop()

    def _log_monitor_loop(self):
        """Reads server output and writes to log file."""
        if self.process is None or self.process.stdout is None:
            return

        try:
            with open(self.log_path, "w", encoding="utf-8") as f:
                for line in iter(self.process.stdout.readline, ""):
                    if not line:
                        break
                    f.write(line)
                    f.flush()
        except Exception as e:
            logger.error(f"Log monitor failed: {e}")

    def _wait_for_health(self, timeout):
        start = time.time()
        url = f"http://{self.host}:{self.port}/health"
        while time.time() - start < timeout:
            if self.process is not None and self.process.poll() is not None:
                return False
            try:
                requests.get(url, timeout=2)
                return True
            except requests.RequestException:
                time.sleep(1)
        return False

    def stop(self):
        if not self.running:
            return
        logger.info("Stopping Whisper Server...")
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

        self.running = False

    def _is_port_in_use(self, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("localhost", port)) == 0
