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

Three-layer port arrangement (per swap)
---------------------------------------
The **public** port is what end users (and CLI tools) target; it's owned by
the CORS shim (``cors_shim.CorsShim``).  llama-swap sits behind the shim on
a loopback-only "listen port" so browsers and partners interacting via HTTP
get the CORS / Local Network Access headers they need without llama-swap
itself implementing CORS.

    public port (self.port)         — owned by CorsShim, browser-facing
    listen port (self._listen_port) — = self.port + _SHIM_OFFSET, llama-swap
                                       loopback-only (127.0.0.1)
    listen port + 100 + i           — llama-server for model[i], loopback-only

The offsets keep three independent layers clear of one another and clear of
neighbouring swaps' ports.  For a default public port of 8100 this lays out
as:

    8100  -> CorsShim
    8150  -> llama-swap
    8250  -> llama-server (model 0)
    8251  -> llama-server (model 1)
    …

is_healthy / _port_in_use / _kill_port all repoint to the listen port so
SwapManager's notion of "is the model server up?" is independent of the
shim — the shim's own lifecycle is owned by ``adapter.LanguageModel``.
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

try:  # package vs. flat import — mirrors adapter.py
    from .cors_shim import CorsShim
except ImportError:  # pragma: no cover - flat layout fallback
    from cors_shim import CorsShim

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
    _SHIM_OFFSET = 50  # llama-swap binds public_port + _SHIM_OFFSET (loopback only)
    _INTERNAL_PORT_OFFSET = 100  # llama-server ports start at listen_port + offset

    def __init__(self, port: int, host: str, bin_dir: Path) -> None:
        self.port = port  # public-facing port (CorsShim binds this)
        # Listen port is where llama-swap itself binds. Always loopback —
        # external clients reach llama-swap exclusively via the shim.
        self._listen_port = port + self._SHIM_OFFSET
        self.host = host
        is_win = platform.system() == "Windows"
        self._swap_bin = bin_dir / ("llama-swap.exe" if is_win else "llama-swap")
        self._server_bin = bin_dir / ("llama-server.exe" if is_win else "llama-server")
        self._config_path = Path("configs") / f"swap_config_{port}.json"
        self._log_path = Path("logs") / f"llama-swap_{port}.log"
        self._models: dict[str, dict] = {}
        self._proc: subprocess.Popen | None = None
        # The browser-facing CORS shim is owned here — one per public port,
        # shared by every serving pinned to it and surviving model reloads —
        # rather than per LanguageModel instance, which used to race two
        # shims for the same port on a second serving.
        self._cors_shim: CorsShim | None = None
        self._lock = threading.RLock()

    @property
    def listen_port(self) -> int:
        """Loopback port llama-swap binds.  CorsShim points its upstream here."""
        return self._listen_port

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
                # Stop the browser-facing shim BEFORE llama-swap so a status
                # poll never briefly hits a shim fronting a dead upstream.
                self._stop_shim()
                self._stop()
                self._config_path.unlink(missing_ok=True)
                with _global_lock:
                    _instances.pop((self.host, self.port), None)
            else:
                self._write_config()
                if self._is_running():
                    self._reload()

    def shutdown(self) -> None:
        """Stop llama-swap (and its CORS shim) unconditionally and drop from the registry."""
        with self._lock:
            self._stop_shim()
            self._stop()
        with _global_lock:
            _instances.pop((self.host, self.port), None)

    @property
    def address(self) -> str:
        return f"http://{self.host}:{self.port}"

    def is_healthy(self) -> bool:
        """Lightweight HTTP liveness check against llama-swap's /health endpoint.

        Hits the loopback ``_listen_port`` directly (bypassing the shim) so
        health reflects the model server independently of shim status.
        ``adapter.LanguageModel`` polls its own shim health separately.
        """
        if self._proc is not None and self._proc.poll() is not None:
            return False  # process already exited — skip the HTTP round-trip
        try:
            resp = requests.get(f"http://127.0.0.1:{self._listen_port}/health", timeout=2)
            return resp.status_code == 200
        except Exception:
            return False

    def ensure_shim(self) -> None:
        """Ensure the CORS shim fronting the public port is running.

        Idempotent and safe to call repeatedly (e.g. from the adapter
        heartbeat): it only acts when llama-swap is actually running, so we
        never strand a shim in front of a dead upstream, and
        ``CorsShim.start()`` no-ops when the shim is already listening — so
        there is no restart-flap log spam. This is the single place a shim is
        created, which is why two servings on one port can no longer race two
        shims for the same socket.
        """
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return  # swap not running — nothing to front
            if self._cors_shim is None:
                self._cors_shim = CorsShim(
                    public_port=self.port,
                    upstream_port=self._listen_port,
                    host=self.host,
                )
            self._cors_shim.start()

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

        # Always kill an existing listener on our port — adopting it would
        # leave the orphan serving with stale config (different model/n_ctx),
        # and we have no _proc handle to SIGHUP for reload.
        if self._port_in_use():
            logger.warning("Killing orphan llama-swap from prior session")
            self._kill_port()
            time.sleep(0.5)
            if self._port_in_use():
                raise RuntimeError("Listen port still held after kill — another Link instance running?")

        logger.info("Starting llama-swap")

        self._log_path.parent.mkdir(exist_ok=True)
        log_fh = open(self._log_path, "a")  # noqa: SIM115 — kept open for subprocess lifetime

        # llama-swap binds 127.0.0.1 exclusively — external clients reach it
        # through CorsShim. self.host is ignored here on purpose.
        self._proc = subprocess.Popen(
            [
                str(self._swap_bin),
                "--config",
                str(self._config_path.resolve()),
                "--listen",
                f"127.0.0.1:{self._listen_port}",
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

        # llama-swap is up — bring the browser-facing CORS shim online in
        # front of it. Idempotent: a model reload re-enters _start while the
        # shim is already listening, so the public port stays up across swaps.
        self.ensure_shim()

        # Surface useful URLs. Public port (CORS-fronted) is for API clients;
        # the llama-swap dev UI lives on the loopback listen port and is only
        # reachable from this machine (CorsShim does not proxy UI paths).
        public_host = "localhost" if self.host in ("0.0.0.0", "127.0.0.1") else self.host
        logger.info(
            "API ready (POST http://%s:%d/v1/chat/completions)",
            public_host,
            self.port,
        )
        logger.info(
            "llama-swap dev UI: http://127.0.0.1:%d (loopback only)",
            self._listen_port,
        )

    def _stop(self) -> None:
        # NOTE: deliberately does NOT stop the shim — _stop is also the
        # reload path on Windows (_reload -> _stop + _start), and the public
        # port must stay up across a model swap. The shim is torn down only
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

    def _stop_shim(self) -> None:
        """Stop and clear the CORS shim if one is running (idempotent)."""
        if self._cors_shim is not None:
            self._cors_shim.stop()
            self._cors_shim = None

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
        # Checks the LISTEN port (where llama-swap binds). The public port
        # is owned by CorsShim and has its own bind guard.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", self._listen_port)) == 0

    def _kill_port(self) -> None:
        """Best-effort kill of whatever process is holding self._listen_port."""
        try:
            import psutil

            for conn in psutil.net_connections(kind="inet"):
                laddr = conn.laddr
                # laddr is `pconn(ip, port)` for INET sockets but pyright sees
                # it as a union with `tuple[()]`; getattr keeps the check safe.
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
            # Internal llama-server ports start at _listen_port + offset
            # (NOT self.port + offset), so the three layers — public ->
            # shim, listen -> swap, listen+100+i -> server — don't overlap.
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
