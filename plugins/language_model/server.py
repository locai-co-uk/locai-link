# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import logging
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


class ModelServer:
    """Manages the llama-server background process."""

    def __init__(self, model_path, host="127.0.0.1", port=8003, on_telemetry=None, **kwargs):
        self.model_path = Path(model_path)
        self.host = host
        self.port = int(port)
        self.process = None
        self.running = False
        self.ready = False
        self._stop_event = threading.Event()

        # Callback for log-based telemetry (used in 'serve' mode)
        self.on_telemetry = on_telemetry

        self.alias = kwargs.get("alias")
        self.n_gpu_layers = int(kwargs.get("n_gpu_layers") or 0)
        self.n_ctx = int(kwargs.get("n_ctx") or 2048)
        self.chat_format = kwargs.get("chat_format")

        # Defer to install.py for binary directory — it already handles FROZEN
        # (PyInstaller bundles), venv, and standalone layouts.  Importing keeps
        # server.py and the install checks in adapter.py looking at the same
        # path; previously they could disagree inside a frozen bundle.
        try:
            from .install import BIN_LLAMA_DIR
        except ImportError:
            from install import BIN_LLAMA_DIR  # type: ignore[no-redef]
        self.bin_dir = BIN_LLAMA_DIR

    @staticmethod
    def build_telemetry_payload(model_id, output_text, start_time, end_time, duration, metadata):
        """
        Standardizes the telemetry payload structure for both Client and Server modes.

        Args:
            model_id (str): The model identifier.
            output_text (str): The generated text or "stats_only".
            start_time (datetime): Start timestamp.
            end_time (datetime): End timestamp.
            duration (float): Duration in seconds.
            metadata (dict): Additional stats (tokens, temperature, source).
        """
        return {
            "model_id": model_id,
            "model_type": "generation",
            "sub_model_type": "text_generation",
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
        binary_name = "llama-server.exe" if system == "Windows" else "llama-server"

        # 1. Check bin-llama
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
            logger.error(f"Inference binary not found in {self.bin_dir}. Run install.py")
            return

        # Show a clickable URL in the log. Wildcard binds (0.0.0.0 / ::) aren't
        # valid connect targets — swap them for `localhost` so the line opens on
        # Windows terminals and browsers too.
        display_host = "localhost" if self.host in ("0.0.0.0", "::", "") else self.host
        logger.info(f"Starting Model Server on http://{display_host}:{self.port}...")

        logs_dir = Path.cwd() / "logs"
        logs_dir.mkdir(exist_ok=True)
        self.log_path = logs_dir / f"server_{self.port}.log"

        # Prepare Environment
        env = os.environ.copy()
        if platform.system() == "Linux":
            bin_dir = server_bin.parent
            lib_paths = {str(bin_dir)}
            # Walk subdirectories to find ggml*.so files (CUDA shared libs)
            for root, _, files in os.walk(bin_dir):
                for file in files:
                    if "ggml" in file and file.endswith(".so"):
                        lib_paths.add(root)
            current_ld = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = os.pathsep.join(list(lib_paths)) + (
                f"{os.pathsep}{current_ld}" if current_ld else ""
            )

        cmd = [
            str(server_bin),
            "--model",
            str(self.model_path),
            "--alias",
            str(self.alias),
            "--host",
            str(self.host),
            "--port",
            str(self.port),
            "--n-gpu-layers",
            str(self.n_gpu_layers),
            "--ctx-size",
            str(self.n_ctx),
            # "--verbose",  # Force verbose logging for telemetry capture
        ]

        # Only pass --chat-template if explicitly configured; otherwise let
        # llama.cpp auto-detect the template from the model's metadata.
        if self.chat_format:
            cmd.extend(["--chat-template", str(self.chat_format)])

        try:
            # We use PIPE for stdout so we can intercept logs in real-time
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Merge stderr into stdout
                env=env,
                text=True,
                bufsize=1,  # Line buffered
            )
            self.running = True
            self.ready = False
            self._stop_event.clear()

            # Start background thread to read logs
            self.monitor_thread = threading.Thread(target=self._log_monitor_loop, daemon=True)
            self.monitor_thread.start()

            # Health check runs in the background so start() can return immediately —
            # otherwise slow-loading models (e.g. cold-cache Windows Defender scan)
            # would block the caller's thread for up to `timeout` seconds.
            self.health_thread = threading.Thread(target=self._health_watcher, daemon=True)
            self.health_thread.start()

            logger.info(f"Server logs: {self.log_path}")

        except Exception as e:
            logger.error(f"Failed to launch server: {e}")
            self.stop()

    def _health_watcher(self):
        """Polls /health until the server responds or times out. Runs on a worker thread."""
        if self._wait_for_health(timeout=120):
            self.ready = True
            logger.info("Server is ready.")
        elif not self._stop_event.is_set():
            # Genuine health failure — not a stop()-triggered cancellation.
            # Surface the server's own log so operators can see WHY it didn't
            # come up (missing DLL, CUDA runtime mismatch, OOM, etc.).
            logger.error("Server failed to respond to health check.", extra={"category": "health"})
            self._log_tail()
            self.stop()

    def _log_tail(self, lines: int = 20):
        """Emit the last N lines of the server's stdout/stderr log to the agent's logger."""
        try:
            if not getattr(self, "log_path", None) or not self.log_path.exists():
                return
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                tail = f.read().splitlines()[-lines:]
            if tail:
                logger.error(f"Last {len(tail)} line(s) from {self.log_path.name}:")
                for line in tail:
                    logger.error(f"  | {line}")
        except Exception as e:
            logger.debug(f"Could not read server log {self.log_path}: {e}")

    def wait_until_ready(self, timeout: float) -> bool:
        """Blocks until the server is healthy, has died, or `timeout` elapses.

        Use when a caller genuinely needs the server up before proceeding — e.g.
        an integration test or a one-shot transcribe/inference call. The pipeline
        loop doesn't need this; it tolerates `ready=False` by emitting nothing.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ready:
                return True
            if not self.running:
                return False
            time.sleep(0.2)
        return False

    def _log_monitor_loop(self):
        """Reads server output, writes to file, and triggers telemetry callback."""
        # --- LINT FIX: Guard clause for NoneType ---
        if self.process is None or self.process.stdout is None:
            return

        try:
            with open(self.log_path, "w", encoding="utf-8") as f:
                # Iterate line by line from the process stdout
                for line in iter(self.process.stdout.readline, ""):
                    if not line:
                        break

                    # 1. Persist log
                    f.write(line)
                    f.flush()

                    # 2. Trigger Telemetry (if callback configured)
                    if self.on_telemetry:
                        self.on_telemetry(line)
        except Exception as e:
            logger.error(f"Log monitor failed: {e}")

    def _wait_for_health(self, timeout):
        start = time.time()
        # 0.0.0.0 / :: are bind addresses, not connect targets. Linux/macOS quietly
        # remap them to loopback; Windows' WSAConnect returns WSAEADDRNOTAVAIL.
        # Always hit the real loopback address for same-host health checks.
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host
        url = f"http://{host}:{self.port}/health"
        while time.time() - start < timeout:
            if self._stop_event.is_set():
                return False
            if self.process is not None and self.process.poll() is not None:
                return False
            try:
                resp = requests.get(url, timeout=1)
                # llama-server returns 503 while the model is still loading. Only
                # 2xx means the model is actually resident and ready for requests.
                if resp.ok:
                    return True
            except requests.RequestException:
                pass
            # Sleep that's interruptible via stop() — so a STOP_SERVING during
            # startup cancels the watcher instead of waiting out the full timeout.
            if self._stop_event.wait(timeout=1.0):
                return False
        return False

    def stop(self):
        if not self.running:
            return
        logger.info("Stopping Model Server...")
        self._stop_event.set()  # Cancel any in-flight health check
        self.ready = False
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

        self.running = False
        # The monitor thread will exit automatically when process stdout closes

    def _is_port_in_use(self, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("localhost", port)) == 0
