# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import os
import platform
import subprocess
import time

from link import logger as link_logger
from link.serving.base_server import BaseServer
from link.utils import PROJECT_ROOT


class WhisperServer(BaseServer):
    """Manages the lifecycle of the local Whisper transcription server using whisper.cpp binaries."""

    def __init__(self, payload: dict):
        """Initialises the Whisper server manager."""
        print("Initialising Whisper Server Manager...")
        super().__init__(payload)

    def _get_server_binary(self):
        system = platform.system()
        binary_name = "whisper-server.exe" if system == "Windows" else "whisper-server"

        candidate = PROJECT_ROOT / ".venv" / "bin-whisper" / binary_name
        if candidate.exists():
            return candidate

        candidate = PROJECT_ROOT / "bin" / binary_name
        if candidate.exists():
            return candidate
        return None

    def start(self):
        """Starts the whisper-server."""
        if not self.is_valid or self.is_running() or self._is_port_in_use(self.port):
            return

        print("Starting Whisper Serving...")
        if not self._load_and_parse_runtime_config():
            return

        if not self.model_path or not self.model_path.exists():
            link_logger.fail(f"Model file not found: {self.model_path}")
            return

        server_bin = self._get_server_binary()
        if not server_bin:
            link_logger.fail("whisper-server binary not found.")
            return

        # Setup Env
        env = os.environ.copy()
        bin_dir = server_bin.parent
        lib_paths = {str(bin_dir)}
        for root, _, files in os.walk(bin_dir):
            for file in files:
                if "whisper" in file and file.endswith(".so"):
                    lib_paths.add(root)

        curr_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = os.pathsep.join(list(lib_paths)) + (f"{os.pathsep}{curr_ld}" if curr_ld else "")

        cmd = [
            str(server_bin),
            "--model",
            str(self.model_path),
            "--host",
            str(self.host),
            "--port",
            str(self.port),
        ]

        param_map = {
            "language": "--language",
            "n_threads": "--threads",
            "beam_size": "--beam-size",
        }

        for k, v in param_map.items():
            if k in self.params:
                cmd.extend([v, str(self.params[k])])

        link_logger.info(f"Launching Whisper server on http://{self.host}:{self.port}")

        try:
            # Open with buffering=1 (line buffered)
            self.log_handle = open(self.log_file, "w", buffering=1)

            self.process = subprocess.Popen(
                cmd, stdout=self.log_handle, stderr=subprocess.STDOUT, cwd=PROJECT_ROOT, env=env, text=True
            )
            self.running = True

            self.pid_file.write_text(str(self.process.pid))
            link_logger.ok(f"Whisper server started (PID {self.process.pid})")

            time.sleep(2)
            if self.process.poll() is not None:
                link_logger.fail("Whisper server crashed immediately. Check logs.")
                self.stop()
                return

        except Exception as e:
            link_logger.fail(f"Failed to start: {e}")
            self.stop()
            return

        ready = self._wait_for_ready(timeout_seconds=60)
        if not ready:
            link_logger.fail("Whisper server did not become ready in time.")

        self._send_status(False, ready)
