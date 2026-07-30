# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Zenoh Python API wrapper — session setup and pub/sub helpers."""

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from link.config.models import TransportArgs

if TYPE_CHECKING:
    import zenoh

logger = logging.getLogger(__name__)


class ZenohClient:
    """Manages the Zenoh session from direct configuration arguments."""

    def __init__(self, args: dict[str, Any] | None = None):
        """Initialises the ZenohClient.

        Args:
            args (dict[str, Any] | None): Dictionary from config.transport.args.
        """
        self.args = args or {}
        self._session: "zenoh.Session | None" = None
        self._zenoh_config = self._build_config()

    def _build_config(self) -> "zenoh.Config":
        """Translates args into Zenoh's internal configuration format."""
        import zenoh  # lazy — triggers the native DLL load only when Zenoh is actually used

        z_conf = zenoh.Config()
        cfg = TransportArgs(**self.args)

        # 1. Mode
        mode = cfg.mode
        z_conf.insert_json5("mode", f'"{mode}"')

        # 2. Endpoints
        endpoints = cfg.endpoints
        if endpoints:
            ep_json = json.dumps(endpoints)
            if mode == "router":
                z_conf.insert_json5("listen/endpoints", ep_json)
            else:
                z_conf.insert_json5("connect/endpoints", ep_json)

        # 3. TLS — gated on endpoint scheme, not mode (a router that federates
        # to a cloud router still dials outbound TLS). Absent/"auto" `tls_root_ca`
        # falls back to certifi (ISRG Root X1 covers the GCE Let's Encrypt cert);
        # set an explicit path only for private/self-signed CAs.
        uses_tls = any(str(ep).startswith("tls/") for ep in endpoints)
        if uses_tls:
            tls_root_ca = cfg.tls_root_ca
            if not tls_root_ca or tls_root_ca == "auto":
                import certifi

                tls_root_ca = certifi.where()
            z_conf.insert_json5(
                "transport/link/tls/root_ca_certificate",
                json.dumps(tls_root_ca),
            )

        # 4. usrpwd auth — username = device_id, password = api_key (or test cred).
        username = cfg.username
        password = cfg.password
        if username and password and uses_tls:
            z_conf.insert_json5(
                "transport/auth/usrpwd",
                json.dumps({"user": username, "password": password}),
            )

        return z_conf

    # zenoh.open() returns before the client has actually connected to the
    # router. Publishing (e.g. the startup "online" lifecycle report) into a
    # not-yet-connected client-mode session is silently dropped — no route,
    # no error. On fast links the connection is up in time; on slower ones
    # (observed on macOS) the first report is lost and the device shows
    # offline while later telemetry still lands. Wait for a connected router
    # before returning, bounded so a genuinely offline start still proceeds.
    _ROUTER_WAIT_SECONDS = 5.0
    _ROUTER_POLL_SECONDS = 0.1

    def get_session(self) -> "zenoh.Session":
        """Returns the active session, opening it if necessary."""
        import zenoh  # lazy — see module docstring

        if self._session:
            return self._session

        # Honour RUST_LOG for the in-process client (no-op unless set), so
        # transport/connection logs can be captured in the field.
        try:
            zenoh.try_init_log_from_env()
        except Exception as e:  # noqa: BLE001 - logging init must never block startup
            logger.debug(f"Zenoh log init skipped: {e}")

        try:
            self._session = zenoh.open(self._zenoh_config)
        except Exception as e:
            # Propagate error so retry logic can handle it
            raise e

        self._wait_for_router(self._session)
        return self._session

    def _wait_for_router(self, session: "zenoh.Session") -> None:
        """Blocks until the session reports a connected router, or the bound
        elapses. Fail-open by design: a probe the zenoh version does not
        support (AttributeError/TypeError) proceeds at once; a transient probe
        error is logged and retried until the deadline, then proceeds."""
        deadline = time.monotonic() + self._ROUTER_WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                if list(session.info().routers_zid()):
                    return
            except (AttributeError, TypeError) as e:
                # The probe API is absent/incompatible: never resolvable, so
                # don't spin the full bound — proceed immediately.
                logger.debug(f"Zenoh readiness probe unsupported, proceeding: {e}")
                return
            except Exception as e:
                # Transient: keep trying until the deadline rather than
                # abandoning the wait on a single hiccup.
                logger.debug(f"Zenoh readiness probe errored, retrying: {e}")
            time.sleep(self._ROUTER_POLL_SECONDS)
        logger.warning(
            f"Zenoh session opened but no router connected within {self._ROUTER_WAIT_SECONDS}s; proceeding anyway."
        )

    def close(self):
        """Closes the current Zenoh session if it exists."""
        if self._session:
            self._session.close()
            self._session = None
