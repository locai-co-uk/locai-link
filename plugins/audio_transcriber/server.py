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

    def __init__(self, model_path, host="127.0.0.1", port=8003, **kwargs):
        self.model_path = Path(model_path)
        self.host = host
        self.port = int(port)
        self.process: subprocess.Popen[str] | None = None
        self.running = False
        self.ready = False
        self._stop_event = threading.Event()
        # Set on the first start(); reset on stop(). Declared here so type
        # checkers don't flag the later assignments as uninitialised.
        self.log_path: Path | None = None
        self.monitor_thread: threading.Thread | None = None
        self.health_thread: threading.Thread | None = None

        self.language = kwargs.get("language")
        self.n_threads = kwargs.get("n_threads")
        self.beam_size = kwargs.get("beam_size")

        # Defer to install.py for binary directory — it already handles FROZEN
        # (PyInstaller bundles), venv, and standalone layouts.
        try:
            from .install import BIN_WHISPER_DIR
        except ImportError:
            from install import BIN_WHISPER_DIR
        self.bin_dir = BIN_WHISPER_DIR

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

        # 3. On-demand fetch from the artifact store — only in a frozen bundle, so
        #    a headless install (no bundled engines) fetches at first use while
        #    dev/source runs keep the bin_dir/install.py path. Soft import: only the
        #    bundled runtime exposes link.infra.engines.
        if getattr(sys, "frozen", False):
            try:
                from link.infra import engines

                return engines.binary_path("whisper-cpp", binary_name)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"on-demand engine fetch unavailable: {e}")

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

        # Show a clickable URL in the log. Wildcard binds (0.0.0.0 / ::) aren't
        # valid connect targets — swap them for `localhost` so the line opens on
        # Windows terminals and browsers too.
        display_host = "localhost" if self.host in ("0.0.0.0", "::", "") else self.host
        logger.info(f"Starting Whisper Server on http://{display_host}:{self.port}...")

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
            logger.error(f"Failed to launch whisper server: {e}")
            self.stop()

    def _health_watcher(self):
        """Polls /health until the server responds or times out. Runs on a worker thread."""
        if self._wait_for_health(timeout=120):
            self.ready = True
            logger.info("Whisper server is ready.")
        elif not self._stop_event.is_set():
            # Genuine health failure — not a stop()-triggered cancellation.
            # Surface the server's own log so operators can see WHY it didn't
            # come up (missing DLL, model-load error, etc.).
            logger.error("Whisper server failed to respond to health check.", extra={"category": "health"})
            self._log_tail()
            self.stop()

    def _log_tail(self, lines: int = 20):
        """Emit the last N lines of the server's stdout/stderr log to the agent's logger."""
        log_path = self.log_path
        if log_path is None or not log_path.exists():
            return
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                tail = f.read().splitlines()[-lines:]
            if tail:
                logger.error(f"Last {len(tail)} line(s) from {log_path.name}:")
                for line in tail:
                    logger.error(f"  | {line}")
        except Exception as e:
            logger.debug(f"Could not read server log {log_path}: {e}")

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
        """Reads server output and writes to log file."""
        if self.process is None or self.process.stdout is None or self.log_path is None:
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
                resp = requests.get(url, timeout=2)
                # Only 2xx means the model is actually loaded and ready. 5xx is
                # typically "still initialising" on these servers.
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
        logger.info("Stopping Whisper Server...")
        self._stop_event.set()  # Cancel any in-flight health check
        self.ready = False
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
