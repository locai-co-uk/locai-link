# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Manages llama-swap subprocesses keyed by (host, port).

One swap per public port; ``maxConcurrent: 1`` keeps a single model in RAM.

Port layout (ServingProxy always fronts llama-swap so telemetry capture
runs regardless of CORS):

    public_port      -> ServingProxy        (CORS + telemetry, both optional)
    public_port + 50 -> llama-swap          (loopback only)
    public_port +150 -> llama-server[i]     (one per registered model)

Orphan handling: each start records its PID in ``state/swap_<port>.pid``;
the next start reclaims its own orphan (cmdline-verified) and refuses to
touch foreign processes. See ``_reclaim_previous_instance``.
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
from collections.abc import Callable
from pathlib import Path

import psutil
import requests

from link.infra.serving_proxy import ServingProxy

logger = logging.getLogger(__name__)

# Process-level registry: one SwapManager per (host, port).
_global_lock = threading.Lock()
_instances: dict[tuple[str, int], "SwapManager"] = {}


def get_swap_manager(
    port: int,
    host: str,
    bin_dir: Path,
    allowed_origins: list[str] | None = None,
    on_telemetry: Callable[[dict], None] | None = None,
) -> "SwapManager":
    """Return the SwapManager for (host, port). ``allowed_origins`` is first-call only;
    ``on_telemetry`` accumulates so multi-model servings each get their callback registered.
    """
    key = (host, port)
    with _global_lock:
        sm = _instances.get(key)
        if sm is None:
            sm = SwapManager(port, host, bin_dir, allowed_origins=allowed_origins)
            _instances[key] = sm
        if on_telemetry is not None:
            sm.add_telemetry_callback(on_telemetry)
        return sm


class SwapManager:
    """Single llama-swap process keyed by (host, port)."""

    _MAX_CONCURRENT = 1
    _HEALTH_CHECK_TIMEOUT = 120
    _MODEL_TTL = 300
    _PROXY_OFFSET = 50
    _INTERNAL_PORT_OFFSET = 100

    def __init__(
        self,
        port: int,
        host: str,
        bin_dir: Path,
        allowed_origins: list[str] | None = None,
    ) -> None:
        self.port = port
        self._allowed_origins: list[str] = [o for o in (allowed_origins or []) if o]
        # ServingProxy is universal — listen_port is always the proxy
        # back-end. CORS is just an optional feature of the proxy.
        self._listen_port = port + self._PROXY_OFFSET
        self.host = host
        is_win = platform.system() == "Windows"
        self._swap_bin = bin_dir / ("llama-swap.exe" if is_win else "llama-swap")
        self._server_bin = bin_dir / ("llama-server.exe" if is_win else "llama-server")
        self._config_path = Path("configs") / f"swap_config_{port}.json"
        self._log_path = Path("logs") / f"llama-swap_{port}.log"
        self._pid_path = Path("state") / f"swap_{port}.pid"
        self._models: dict[str, dict] = {}
        self._proc: subprocess.Popen | None = None
        self._proxy: ServingProxy | None = None
        # One callback per adapter; each filters by record["model"] so two
        # adapters sharing one port don't mis-attribute each other's inferences.
        self._on_telemetry_callbacks: list[Callable[[dict], None]] = []
        self._lock = threading.RLock()

    def add_telemetry_callback(self, cb: Callable[[dict], None]) -> None:
        """Register an inference-telemetry sink. Idempotent."""
        with self._lock:
            if cb not in self._on_telemetry_callbacks:
                self._on_telemetry_callbacks.append(cb)

    def _fanout_telemetry(self, record: dict) -> None:
        """ServingProxy hands each inference record here; we fan out to every
        registered adapter. Adapters filter by record["model"] internally."""
        with self._lock:
            callbacks = list(self._on_telemetry_callbacks)
        for cb in callbacks:
            try:
                cb(record)
            except Exception as exc:
                logger.debug("telemetry callback raised: %s", exc)

    @property
    def listen_port(self) -> int:
        return self._listen_port

    @property
    def cors_enabled(self) -> bool:
        """True when ACAO headers will be emitted (allowlist non-empty)."""
        return bool(self._allowed_origins)

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
                # Proxy first, then llama-swap, so a status poll never hits
                # a proxy fronting a dead upstream.
                self._stop_proxy()
                self._stop()
                self._config_path.unlink(missing_ok=True)
                with _global_lock:
                    _instances.pop((self.host, self.port), None)
            else:
                self._write_config()
                if self._is_running():
                    self._reload()

    def shutdown(self) -> None:
        with self._lock:
            self._stop_proxy()
            self._stop()
        with _global_lock:
            _instances.pop((self.host, self.port), None)

    @property
    def address(self) -> str:
        return f"http://{self.host}:{self.port}"

    def is_healthy(self) -> bool:
        """Hit llama-swap's /health on the listen port (bypasses the proxy)."""
        if self._proc is not None and self._proc.poll() is not None:
            return False
        try:
            resp = requests.get(f"http://127.0.0.1:{self._listen_port}/health", timeout=2)
            return resp.status_code == 200
        except Exception:
            return False

    def ensure_proxy(self) -> None:
        """Idempotent. No-op when llama-swap isn't running."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return  # swap not running — nothing to front
            if self._proxy is None:
                self._proxy = ServingProxy(
                    public_port=self.port,
                    upstream_port=self._listen_port,
                    allowed_origins=self._allowed_origins,
                    on_telemetry=self._fanout_telemetry,
                    host=self.host,
                )
            self._proxy.start()

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

        self._reclaim_previous_instance()
        if self._port_in_use():
            raise RuntimeError(
                f"Listen port {self._listen_port} is held by a process we don't own (no pidfile). "
                f"Investigate with `lsof -i :{self._listen_port}` (Linux/macOS) or "
                f"`netstat -ano | findstr {self._listen_port}` (Windows) and clear it before retrying."
            )

        logger.info(
            f"Starting llama-swap on http://127.0.0.1:{self._listen_port} "
            f"(public port {self.port} fronted by ServingProxy)"
        )
        swap_bind = f"127.0.0.1:{self._listen_port}"

        self._log_path.parent.mkdir(exist_ok=True)
        # Log file is kept for human troubleshooting; telemetry comes from ServingProxy.
        log_fh = open(self._log_path, "a")  # noqa: SIM115 — kept open for subprocess lifetime
        self._proc = subprocess.Popen(
            [
                str(self._swap_bin),
                "--config",
                str(self._config_path.resolve()),
                "--listen",
                swap_bind,
            ],
            stdout=log_fh,
            stderr=log_fh,
        )
        self._write_pid(self._proc.pid)

        # 1s settle — if it exits this fast, the config / listen port is bad.
        time.sleep(1)
        if self._proc.poll() is not None:
            log_fh.flush()
            self._clear_pid()
            try:
                tail = self._log_path.read_text(errors="replace").splitlines()[-10:]
                logger.error("llama-swap exited immediately. Last log lines:")
                for line in tail:
                    logger.error(f"  | {line}")
            except Exception:
                pass
            raise RuntimeError(f"llama-swap failed to start (exit {self._proc.returncode}). See {self._log_path}")

        self.ensure_proxy()

    def _stop(self) -> None:
        # NOT responsible for the proxy — _stop is also the Windows reload
        # path (_stop + _start) and the public port must stay up across model
        # swaps. Proxy teardown happens in remove_model / shutdown.
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
        self._clear_pid()

    def _stop_proxy(self) -> None:
        if self._proxy is not None:
            self._proxy.stop()
            self._proxy = None

    def _reload(self) -> None:
        """SIGHUP on Linux/macOS; stop+start on Windows (no SIGHUP equivalent)."""
        assert self._proc is not None
        if platform.system() == "Windows":
            self._stop()
            self._start()
        else:
            self._proc.send_signal(signal.SIGHUP)

    def _port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", self._listen_port)) == 0

    # ------------------------------------------------------------------
    # Pidfile + orphan reclaim
    # ------------------------------------------------------------------

    def _write_pid(self, pid: int) -> None:
        self._pid_path.parent.mkdir(parents=True, exist_ok=True)
        self._pid_path.write_text(str(pid), encoding="utf-8")

    def _read_pid(self) -> int | None:
        try:
            return int(self._pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _clear_pid(self) -> None:
        try:
            self._pid_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug(f"Failed to remove pidfile {self._pid_path}: {exc}")

    def _reclaim_previous_instance(self) -> None:
        """Terminate our previous orphan (pidfile + cmdline match); skip on mismatch."""
        prev_pid = self._read_pid()
        if prev_pid is None:
            return

        try:
            proc = psutil.Process(prev_pid)
        except psutil.NoSuchProcess:
            logger.info(f"Stale pidfile (PID {prev_pid} not running); cleaning up")
            self._clear_pid()
            return
        except psutil.Error as exc:
            logger.warning(f"Cannot inspect PID {prev_pid}: {exc}. Leaving alone.")
            self._clear_pid()
            return

        if not self._looks_like_llama_swap(proc):
            logger.warning(f"Pidfile PID {prev_pid} isn't llama-swap (likely PID reuse); cleaning up")
            self._clear_pid()
            return

        logger.info(f"Reclaiming previous llama-swap (PID {prev_pid})")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                logger.warning(f"PID {prev_pid} didn't exit on SIGTERM; sending SIGKILL")
                proc.kill()
                proc.wait(timeout=2)
        except psutil.NoSuchProcess:
            # Race between inspect and terminate — process already gone.
            # That's the desired end state, fall through to pidfile cleanup.
            logger.debug(f"PID {prev_pid} exited during reclaim — already gone")
        except psutil.Error as exc:
            logger.error(f"Failed to terminate PID {prev_pid}: {exc}")
            # Leave the pidfile so the next attempt retries.
            return

        self._clear_pid()

    @staticmethod
    def _looks_like_llama_swap(proc: psutil.Process) -> bool:
        """Process name OR cmdline contains 'llama-swap'."""
        try:
            if "llama-swap" in proc.name().lower():
                return True
            return any("llama-swap" in arg.lower() for arg in proc.cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    # ------------------------------------------------------------------
    # Config generation
    # ------------------------------------------------------------------

    def _write_config(self) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)

        model_entries: dict = {}
        for i, (model_id, m) in enumerate(self._models.items()):
            internal_port = self._listen_port + self._INTERNAL_PORT_OFFSET + i
            args = [str(self._server_bin), "--model", m["path"], "--port", str(internal_port)] + m["args"]
            cmd = subprocess.list2cmdline(args) if platform.system() == "Windows" else shlex.join(args)
            entry: dict = {
                "cmd": cmd,
                "proxy": f"http://127.0.0.1:{internal_port}",
                "ttl": self._MODEL_TTL,
            }
            if m["env"]:
                # llama-swap expects env as ["KEY=VAL", ...] not a map.
                entry["env"] = [f"{k}={v}" for k, v in m["env"].items()]
            model_entries[model_id] = entry

        config = {
            "healthCheckTimeout": self._HEALTH_CHECK_TIMEOUT,
            "maxConcurrent": self._MAX_CONCURRENT,
            "models": model_entries,
        }
        self._config_path.write_text(json.dumps(config, indent=2))
