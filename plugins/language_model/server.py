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


def resolve_engine_binary(engine: str, filename: str, bin_dir: Path) -> Path | None:
    """Resolve an engine binary: bundled bin dir, then PATH, then (frozen
    bundles only) an on-demand artifact-store fetch into the engine cache.
    Prefetch builds resolve at step 1, so the store cache is never duplicated
    beside a bundled copy."""
    candidate = bin_dir / filename
    if candidate.exists():
        return candidate

    path_bin = shutil.which(filename)
    if path_bin:
        return Path(path_bin)

    # Soft import: only the bundled runtime exposes link.infra.engines; source
    # runs keep the bin_dir/install.py path.
    if getattr(sys, "frozen", False):
        try:
            from link.infra import engines

            return engines.binary_path(engine, filename)
        except Exception as e:  # noqa: BLE001
            # Surface the reason (store 404, network, bad hash): this is the
            # last resolution step, so a silent fall-through reads as
            # "binary just missing" with no diagnosable cause.
            logger.warning(f"on-demand {engine} fetch failed: {e}")

    return None


class ModelServer:
    """Manages the llama-server background process."""

    def __init__(self, model_path, host="127.0.0.1", port=8003, on_telemetry=None, **kwargs):
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

        self.on_telemetry = on_telemetry  # log-based telemetry callback (serve mode)

        self.alias = kwargs.get("alias")
        self.n_gpu_layers = int(kwargs.get("n_gpu_layers") or 0)
        self.n_ctx = int(kwargs.get("n_ctx") or 2048)
        self.chat_format = kwargs.get("chat_format")

        # install.py already handles FROZEN / venv / standalone layouts —
        # share its BIN_LLAMA_DIR so frozen bundles don't disagree with it.
        try:
            from .install import BIN_LLAMA_DIR
        except ImportError:
            from install import BIN_LLAMA_DIR
        self.bin_dir = BIN_LLAMA_DIR

    @staticmethod
    def build_telemetry_payload(model_id, output_text, start_time, end_time, duration, metadata):
        """Shared telemetry payload shape for client + server modes."""
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
        binary_name = "llama-server.exe" if platform.system() == "Windows" else "llama-server"
        return resolve_engine_binary("llama-cpp", binary_name, self.bin_dir)

    def start(self):
        if self.running:
            return

        if self._is_port_in_use(self.port):
            logger.error(f"Port {self.port} is already in use!")
            return

        server_bin = self._get_server_binary()
        if not server_bin or not server_bin.exists():
            # Raise, don't return: a silent return here let the pipeline report
            # "Serving started" with no engine on disk and nothing listening.
            raise RuntimeError(
                f"inference engine (llama-server) not found in {self.bin_dir}, "
                "on PATH, or via the artifact store - cannot serve"
            )

        # Wildcard binds aren't clickable; show localhost in the log line.
        display_host = "localhost" if self.host in ("0.0.0.0", "::", "") else self.host
        logger.info(f"Starting Model Server on http://{display_host}:{self.port}...")

        logs_dir = Path.cwd() / "logs"
        logs_dir.mkdir(exist_ok=True)
        self.log_path = logs_dir / f"server_{self.port}.log"

        env = os.environ.copy()
        if platform.system() == "Linux":
            bin_dir = server_bin.parent
            lib_paths = {str(bin_dir)}
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
        ]
        # Without --chat-template, llama.cpp auto-detects from model metadata.
        if self.chat_format:
            cmd.extend(["--chat-template", str(self.chat_format)])

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

            self.monitor_thread = threading.Thread(target=self._log_monitor_loop, daemon=True)
            self.monitor_thread.start()
            # Health watcher runs in the background so start() returns immediately —
            # slow-loading models (Windows Defender scan, cold cache) would otherwise
            # block the caller's thread for up to the timeout.
            self.health_thread = threading.Thread(target=self._health_watcher, daemon=True)
            self.health_thread.start()

            logger.info(f"Server logs: {self.log_path}")

        except Exception as e:
            logger.error(f"Failed to launch server: {e}")
            self.stop()

    def _health_watcher(self):
        """Background poll of /health; on failure (not stop()), dump the log tail."""
        if self._wait_for_health(timeout=120):
            self.ready = True
            logger.info("Server is ready.")
        elif not self._stop_event.is_set():
            logger.error("Server failed to respond to health check.", extra={"category": "health"})
            self._log_tail()
            self.stop()

    def _log_tail(self, lines: int = 20):
        """Emit the last N lines of the server's log to the agent's logger."""
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
        """Block until the server is healthy, has died, or ``timeout`` elapses."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ready:
                return True
            if not self.running:
                return False
            time.sleep(0.2)
        return False

    def _log_monitor_loop(self):
        """Persist server stdout to file and fan each line out to on_telemetry."""
        if self.process is None or self.process.stdout is None or self.log_path is None:
            return

        try:
            with open(self.log_path, "w", encoding="utf-8") as f:
                for line in iter(self.process.stdout.readline, ""):
                    if not line:
                        break
                    f.write(line)
                    f.flush()
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
