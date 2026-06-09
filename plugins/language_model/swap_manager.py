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

Port arrangement
----------------
Two layouts are possible per swap, decided once at construction based on
whether the adapter passed a non-empty CORS allowlist:

**Without CORS (default — zero added overhead):**

    public port (self.port)      -> llama-swap binds directly
    public port + 100 + i        -> llama-server for model[i], loopback-only

No proxy in the request path. Identical wire path to pre-shim code, for
CLI / native HTTP callers that don't need cross-origin browser support.

**With CORS (`allowed_origins` non-empty):**

    public port (self.port)             -> CorsProxy (browser-facing, CORS)
    listen port (self.port + 50)        -> llama-swap on 127.0.0.1 only
    listen port + 100 + i               -> llama-server for model[i]

The CORS proxy owns the public port; llama-swap moves to a loopback-only
internal port so browsers and partners interacting via HTTP get the CORS
/ Local Network Access headers they need without llama-swap itself
implementing CORS.

The choice is read once from the adapter — there is no per-request
branching on the hot path.
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

from link.infra.cors_proxy import CorsProxy

logger = logging.getLogger(__name__)

# Process-level registry: one SwapManager per (host, port).
_global_lock = threading.Lock()
_instances: dict[tuple[str, int], "SwapManager"] = {}


def get_swap_manager(
    port: int,
    host: str,
    bin_dir: Path,
    allowed_origins: list[str] | None = None,
) -> "SwapManager":
    """Return the SwapManager for (host, port), creating it on first use.

    ``allowed_origins`` is honoured only on the *first* call for a given
    (host, port): once the swap exists, subsequent servings share its
    CORS configuration. That matches the production case where every
    serving on a port belongs to the same deployment.
    """
    key = (host, port)
    with _global_lock:
        sm = _instances.get(key)
        if sm is None:
            sm = SwapManager(port, host, bin_dir, allowed_origins=allowed_origins)
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
    _PROXY_OFFSET = 50  # when CORS is on, llama-swap binds public_port + offset (loopback only)
    _INTERNAL_PORT_OFFSET = 100  # llama-server ports start at listen_port + offset

    def __init__(
        self,
        port: int,
        host: str,
        bin_dir: Path,
        allowed_origins: list[str] | None = None,
    ) -> None:
        self.port = port  # public-facing port
        # When CORS is enabled, the public port is owned by CorsProxy and
        # llama-swap moves to a loopback offset. When CORS is off, llama-swap
        # binds the public port directly — same wire path as pre-shim code.
        self._allowed_origins: list[str] = [o for o in (allowed_origins or []) if o]
        self._cors_enabled = bool(self._allowed_origins)
        self._listen_port = port + self._PROXY_OFFSET if self._cors_enabled else port
        self.host = host
        is_win = platform.system() == "Windows"
        self._swap_bin = bin_dir / ("llama-swap.exe" if is_win else "llama-swap")
        self._server_bin = bin_dir / ("llama-server.exe" if is_win else "llama-server")
        self._config_path = Path("configs") / f"swap_config_{port}.json"
        self._log_path = Path("logs") / f"llama-swap_{port}.log"
        self._models: dict[str, dict] = {}
        self._proc: subprocess.Popen | None = None
        # The browser-facing CORS proxy is owned here — one per public port
        # when CORS is enabled, shared by every serving pinned to it and
        # surviving model reloads. ``None`` when CORS is disabled (zero-cost
        # path: llama-swap binds the public port directly).
        self._cors_proxy: CorsProxy | None = None
        self._lock = threading.RLock()

    @property
    def listen_port(self) -> int:
        """Port llama-swap binds.  When CORS is enabled this is the loopback
        offset that the proxy points at; when CORS is disabled it equals the
        public port (llama-swap binds it directly).
        """
        return self._listen_port

    @property
    def cors_enabled(self) -> bool:
        """True when this swap has an active CORS proxy in front of it."""
        return self._cors_enabled

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
                # Stop the browser-facing proxy BEFORE llama-swap so a status
                # poll never briefly hits a proxy fronting a dead upstream.
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
        """Stop llama-swap (and its CORS proxy if any) and drop from the registry."""
        with self._lock:
            self._stop_proxy()
            self._stop()
        with _global_lock:
            _instances.pop((self.host, self.port), None)

    @property
    def address(self) -> str:
        return f"http://{self.host}:{self.port}"

    def is_healthy(self) -> bool:
        """Lightweight HTTP liveness check against llama-swap's /health endpoint.

        Hits the listen port directly (bypassing any CORS proxy) so health
        reflects the model server independently of proxy status.
        """
        if self._proc is not None and self._proc.poll() is not None:
            return False  # process already exited — skip the HTTP round-trip
        try:
            resp = requests.get(f"http://127.0.0.1:{self._listen_port}/health", timeout=2)
            return resp.status_code == 200
        except Exception:
            return False

    def ensure_proxy(self) -> None:
        """Ensure the CORS proxy fronting the public port is running.

        No-op when CORS is disabled for this swap (the zero-cost path —
        llama-swap binds the public port directly, no proxy ever exists).

        When CORS is enabled, this is idempotent and safe to call repeatedly
        (e.g. from the adapter heartbeat): it only acts when llama-swap is
        actually running, so we never strand a proxy in front of a dead
        upstream, and ``CorsProxy.start()`` no-ops when already listening —
        so there is no restart-flap log spam. This is the single place a
        proxy is created, which is why two servings on one port can no
        longer race two proxies for the same socket.
        """
        with self._lock:
            if not self._cors_enabled:
                return
            if self._proc is None or self._proc.poll() is not None:
                return  # swap not running — nothing to front
            if self._cors_proxy is None:
                self._cors_proxy = CorsProxy(
                    public_port=self.port,
                    upstream_port=self._listen_port,
                    allowed_origins=self._allowed_origins,
                    host=self.host,
                )
            self._cors_proxy.start()

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
        # that is still holding our listen port — new process can't bind
        # otherwise.
        if self._port_in_use():
            if self.is_healthy():
                logger.warning(
                    f"Listen port {self._listen_port} already has a healthy llama-swap; "
                    "reusing it. Send STOP_SERVING first if you need a clean restart."
                )
                return
            logger.warning(f"Listen port {self._listen_port} is in use but not healthy — killing stale process")
            self._kill_port()

        if self._cors_enabled:
            logger.info(
                f"Starting llama-swap on http://127.0.0.1:{self._listen_port} "
                f"(public port {self.port} fronted by CorsProxy)"
            )
            swap_bind = f"127.0.0.1:{self._listen_port}"
        else:
            logger.info(f"Starting llama-swap on http://{self.host}:{self._listen_port}")
            swap_bind = f"{self.host}:{self._listen_port}"

        self._log_path.parent.mkdir(exist_ok=True)
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

        # Brief sanity check — if the process exits within 1 s the config or
        # listen port caused an immediate failure; surface the last log lines.
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

        # llama-swap is up — bring the CORS proxy online if configured.
        # No-op when CORS is disabled.
        self.ensure_proxy()

    def _stop(self) -> None:
        # NOTE: deliberately does NOT stop the proxy — _stop is also the
        # reload path on Windows (_reload -> _stop + _start), and the public
        # port must stay up across a model swap. The proxy is torn down only
        # when the last model is removed (see remove_model / shutdown).
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

    def _stop_proxy(self) -> None:
        """Stop and clear the CORS proxy if one is running (idempotent)."""
        if self._cors_proxy is not None:
            self._cors_proxy.stop()
            self._cors_proxy = None

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
            return s.connect_ex(("127.0.0.1", self._listen_port)) == 0

    def _kill_port(self) -> None:
        """Best-effort kill of whatever process is holding self._listen_port."""
        try:
            import psutil

            for conn in psutil.net_connections(kind="inet"):
                laddr = conn.laddr
                if not laddr or getattr(laddr, "port", None) != self._listen_port or not conn.pid:
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
            # Internal llama-server ports start at _listen_port + offset, so
            # whichever layout is in use (with or without proxy), the
            # internal layer never overlaps with the listen port.
            internal_port = self._listen_port + self._INTERNAL_PORT_OFFSET + i
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
