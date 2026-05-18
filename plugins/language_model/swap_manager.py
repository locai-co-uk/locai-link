# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""SwapManager — singleton that owns the llama-swap process and its JSON config.

All language-model pipelines in serve mode register here.  llama-swap keeps a
single listener port per device and loads/unloads individual llama-server
instances on demand (maxConcurrent: 1), so there is never more than one model
resident in RAM at a time without link needing to deactivate pipelines manually.

Internal port layout
--------------------
swap port  (e.g. 8100) — externally visible, owned by llama-swap
swap port+1            — llama-server for model[0]
swap port+2            — llama-server for model[1]
…and so on.

These internal ports are only reachable from localhost; llama-swap proxies to
them and they are reassigned each time the config is (re)written.
"""

import json
import logging
import platform
import shlex
import signal
import subprocess
import threading
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# Process-level singleton — one swap process per link runtime.
_global_lock = threading.Lock()
_instance: "SwapManager | None" = None


def get_swap_manager(port: int, host: str, bin_dir: Path) -> "SwapManager":
    """Return the module-level singleton, creating or restarting it as needed."""
    global _instance
    with _global_lock:
        if _instance is None:
            _instance = SwapManager(port, host, bin_dir)
        elif not _instance._matches(port, host):
            # Port/host changed (shouldn't happen; single device, single port).
            _instance.shutdown()
            _instance = SwapManager(port, host, bin_dir)
        return _instance


class SwapManager:
    """Manages a single llama-swap process for this device.

    Config is written to configs/swap_config.json (project-local, same
    directory as session state files).  The file is removed when the last
    model is deregistered.
    """

    _CONFIG_PATH = Path("configs") / "swap_config.json"
    _MAX_CONCURRENT = 1
    _HEALTH_CHECK_TIMEOUT = 120  # seconds llama-swap waits for a model to become ready

    def __init__(self, port: int, host: str, bin_dir: Path) -> None:
        self.port = port
        self.host = host
        is_win = platform.system() == "Windows"
        self._swap_bin = bin_dir / ("llama-swap.exe" if is_win else "llama-swap")
        self._server_bin = bin_dir / ("llama-server.exe" if is_win else "llama-server")
        self._models: dict[str, dict] = {}  # model_id -> {path, args, env}
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_model(
        self,
        model_id: str,
        model_path: str,
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Register a model and reload (or start) llama-swap."""
        with self._lock:
            self._models[model_id] = {
                "path": model_path,
                "args": extra_args or [],
                "env": env or {},
            }
            self._write_config()
            if self._is_running():
                self._reload()
            else:
                self._start()

    def remove_model(self, model_id: str) -> None:
        """Deregister a model.  Stops llama-swap when no models remain."""
        with self._lock:
            if model_id not in self._models:
                return
            del self._models[model_id]
            if not self._models:
                self._stop()
                self._CONFIG_PATH.unlink(missing_ok=True)
            else:
                self._write_config()
                if self._is_running():
                    self._reload()

    def shutdown(self) -> None:
        """Stop llama-swap unconditionally (called on runtime shutdown)."""
        with self._lock:
            self._stop()

    @property
    def address(self) -> str:
        return f"http://{self.host}:{self.port}"

    def is_healthy(self) -> bool:
        """Lightweight HTTP liveness check against llama-swap's /health endpoint."""
        try:
            resp = requests.get(f"{self.address}/health", timeout=2)
            return resp.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Process lifecycle (all called under self._lock)
    # ------------------------------------------------------------------

    def _matches(self, port: int, host: str) -> bool:
        return self.port == port and self.host == host

    def _is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start(self) -> None:
        if not self._swap_bin.exists():
            raise RuntimeError(
                f"llama-swap binary not found at {self._swap_bin}. "
                "Re-run plugin install or check language_model installation."
            )
        display_host = "localhost" if self.host in ("0.0.0.0", "::", "") else self.host
        logger.info(f"Starting llama-swap on http://{display_host}:{self.port}")
        self._proc = subprocess.Popen(
            [
                str(self._swap_bin),
                "--config",
                str(self._CONFIG_PATH),
                "--listen",
                f"{self.host}:{self.port}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _stop(self) -> None:
        if not self._is_running():
            return
        logger.info("Stopping llama-swap")
        assert self._proc is not None
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        self._proc = None

    def _reload(self) -> None:
        """Ask llama-swap to reload its config file.

        Linux/macOS: SIGHUP triggers an in-process reload without dropping
        connections.  Windows has no SIGHUP equivalent — restart the process.
        """
        assert self._proc is not None
        if platform.system() == "Windows":
            self._stop()
            self._start()
        else:
            self._proc.send_signal(signal.SIGHUP)

    # ------------------------------------------------------------------
    # Config generation
    # ------------------------------------------------------------------

    def _write_config(self) -> None:
        """(Over)write the JSON config consumed by llama-swap."""
        self._CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

        model_entries: dict = {}
        for i, (model_id, m) in enumerate(self._models.items()):
            internal_port = self.port + 1 + i
            args = (
                [str(self._server_bin), "--model", m["path"], "--port", str(internal_port)]
                + m["args"]
            )
            cmd = (
                subprocess.list2cmdline(args)
                if platform.system() == "Windows"
                else shlex.join(args)
            )
            entry: dict = {
                "cmd": cmd,
                "proxy": f"http://127.0.0.1:{internal_port}",
                "ttl": 60,
            }
            if m["env"]:
                entry["env"] = m["env"]
            model_entries[model_id] = entry

        config = {
            "healthCheckTimeout": self._HEALTH_CHECK_TIMEOUT,
            "maxConcurrent": self._MAX_CONCURRENT,
            "models": model_entries,
        }
        self._CONFIG_PATH.write_text(json.dumps(config, indent=2))
