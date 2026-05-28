# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""SwapManager — manages llama-swap processes keyed by external port.

llama-swap is a small proxy that loads/unloads llama-server instances on demand,
keeping one listener port per swap with `maxConcurrent: 1` so only one model
sits in RAM at a time.

link uses one SwapManager per (host, port).  In normal operation the frontend
pins every model to the same port and they all share a single swap.  The
registry below transparently supports multiple ports as a fallback, so a
stray second port from the control plane gets its own swap instead of tearing
down the first one (which left earlier adapters polling a dead process).

Internal port layout (per swap)
-------------------------------
swap port              — externally visible, owned by llama-swap
swap port + 100 + i    — llama-server for model[i]

The +100 offset keeps each swap's internal ports clear of neighbouring swaps'
external ports.  Internal ports are reachable only from localhost.
"""

import json
import logging
import platform
import shlex
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# Process-level registry: one SwapManager per (host, port).
_global_lock = threading.Lock()
_instances: dict[tuple[str, int], "SwapManager"] = {}


def get_swap_manager(port: int, host: str, bin_dir: Path) -> "SwapManager":
    """Return the SwapManager for (host, port), creating it on first use."""
    key = (host, port)
    with _global_lock:
        sm = _instances.get(key)
        if sm is None:
            sm = SwapManager(port, host, bin_dir)
            _instances[key] = sm
        return sm


class SwapManager:
    """Manages a single llama-swap process for one (host, port) pair.

    Config and log files are keyed by port (configs/swap_config_<port>.json,
    logs/llama-swap_<port>.log) so two swaps in the same process don't collide.
    """

    _MAX_CONCURRENT = 1
    _HEALTH_CHECK_TIMEOUT = 120  # seconds llama-swap waits for a model to become ready
    _MODEL_TTL = 300  # seconds a model stays loaded after the last request
    _INTERNAL_PORT_OFFSET = 100  # internal llama-server ports start at swap_port + offset

    def __init__(self, port: int, host: str, bin_dir: Path) -> None:
        self.port = port
        self.host = host
        is_win = platform.system() == "Windows"
        self._swap_bin = bin_dir / ("llama-swap.exe" if is_win else "llama-swap")
        self._server_bin = bin_dir / ("llama-server.exe" if is_win else "llama-server")
        self._config_path = Path("configs") / f"swap_config_{port}.json"
        self._log_path = Path("logs") / f"llama-swap_{port}.log"
        self._models: dict[str, dict] = {}
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
                self._config_path.unlink(missing_ok=True)
                with _global_lock:
                    _instances.pop((self.host, self.port), None)
            else:
                self._write_config()
                if self._is_running():
                    self._reload()

    def shutdown(self) -> None:
        """Stop llama-swap unconditionally and drop from the registry."""
        with self._lock:
            self._stop()
        with _global_lock:
            _instances.pop((self.host, self.port), None)

    @property
    def address(self) -> str:
        return f"http://{self.host}:{self.port}"

    def is_healthy(self) -> bool:
        """Lightweight HTTP liveness check against llama-swap's /health endpoint."""
        if self._proc is not None and self._proc.poll() is not None:
            return False  # process already exited — skip the HTTP round-trip
        # 0.0.0.0 / :: are bind addresses — not valid connect targets.
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host
        try:
            resp = requests.get(f"http://{host}:{self.port}/health", timeout=2)
            return resp.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Process lifecycle (all called under self._lock)
    # ------------------------------------------------------------------

    def _is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start(self) -> None:
        if not self._swap_bin.exists():
            raise RuntimeError(
                f"llama-swap binary not found at {self._swap_bin}. "
                "Re-run plugin install or check language_model installation."
            )

        # Kill any stale llama-swap left over from a previous unclean shutdown
        # that is still holding our port — new process can't bind otherwise.
        if self._port_in_use():
            if self.is_healthy():
                logger.warning(
                    f"Port {self.port} already has a healthy llama-swap; reusing it. "
                    "Send STOP_SERVING first if you need a clean restart."
                )
                return
            logger.warning(f"Port {self.port} is in use but not healthy — killing stale process")
            self._kill_port()

        display_host = "localhost" if self.host in ("0.0.0.0", "::", "") else self.host
        logger.info(f"Starting llama-swap on http://{display_host}:{self.port}")

        self._log_path.parent.mkdir(exist_ok=True)
        log_fh = open(self._log_path, "a")  # noqa: SIM115 — kept open for subprocess lifetime

        self._proc = subprocess.Popen(
            [
                str(self._swap_bin),
                "--config",
                str(self._config_path.resolve()),
                "--listen",
                f"{self.host}:{self.port}",
            ],
            stdout=log_fh,
            stderr=log_fh,
        )

        # Brief sanity check — if the process exits within 1 s the config or
        # port caused an immediate failure; surface the last log lines.
        time.sleep(1)
        if self._proc.poll() is not None:
            log_fh.flush()
            try:
                tail = self._log_path.read_text(errors="replace").splitlines()[-10:]
                logger.error("llama-swap exited immediately. Last log lines:")
                for line in tail:
                    logger.error(f"  | {line}")
            except Exception:
                pass
            raise RuntimeError(f"llama-swap failed to start (exit {self._proc.returncode}). See {self._log_path}")

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

    def _port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def _kill_port(self) -> None:
        """Best-effort kill of whatever process is holding self.port."""
        try:
            import psutil

            for conn in psutil.net_connections(kind="inet"):
                laddr = conn.laddr
                # laddr is `pconn(ip, port)` for INET sockets but pyright sees
                # it as a union with `tuple[()]`; getattr keeps the check safe.
                if not laddr or getattr(laddr, "port", None) != self.port or not conn.pid:
                    continue
                try:
                    psutil.Process(conn.pid).terminate()
                    time.sleep(0.5)
                except Exception:
                    pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Config generation
    # ------------------------------------------------------------------

    def _write_config(self) -> None:
        """(Over)write the JSON config consumed by llama-swap."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)

        model_entries: dict = {}
        for i, (model_id, m) in enumerate(self._models.items()):
            internal_port = self.port + self._INTERNAL_PORT_OFFSET + i
            args = [str(self._server_bin), "--model", m["path"], "--port", str(internal_port)] + m["args"]
            cmd = subprocess.list2cmdline(args) if platform.system() == "Windows" else shlex.join(args)
            entry: dict = {
                "cmd": cmd,
                "proxy": f"http://127.0.0.1:{internal_port}",
                "ttl": self._MODEL_TTL,
            }
            if m["env"]:
                # llama-swap expects env as []string of "KEY=VALUE" entries, not a map.
                entry["env"] = [f"{k}={v}" for k, v in m["env"].items()]
            model_entries[model_id] = entry

        config = {
            "healthCheckTimeout": self._HEALTH_CHECK_TIMEOUT,
            "maxConcurrent": self._MAX_CONCURRENT,
            "models": model_entries,
        }
        self._config_path.write_text(json.dumps(config, indent=2))
