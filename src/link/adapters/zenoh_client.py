# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Zenoh Python API wrapper — session setup and pub/sub helpers."""

import json
import logging
from typing import TYPE_CHECKING, Any

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

        # 1. Mode
        mode = self.args.get("mode", "client")
        z_conf.insert_json5("mode", f'"{mode}"')

        # 2. Endpoints
        endpoints = self.args.get("endpoints", [])
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
            tls_root_ca = self.args.get("tls_root_ca")
            if not tls_root_ca or tls_root_ca == "auto":
                import certifi

                tls_root_ca = certifi.where()
            z_conf.insert_json5(
                "transport/link/tls/root_ca_certificate",
                json.dumps(tls_root_ca),
            )

        # 4. usrpwd auth — username = device_id, password = api_key (or test cred).
        username = self.args.get("username")
        password = self.args.get("password")
        if username and password and uses_tls:
            z_conf.insert_json5(
                "transport/auth/usrpwd",
                json.dumps({"user": username, "password": password}),
            )

        return z_conf

    def get_session(self) -> "zenoh.Session":
        """Returns the active session, opening it if necessary."""
        import zenoh  # lazy — see module docstring

        if self._session:
            return self._session

        try:
            self._session = zenoh.open(self._zenoh_config)
            return self._session
        except Exception as e:
            # Propagate error so retry logic can handle it
            raise e

    def close(self):
        """Closes the current Zenoh session if it exists."""
        if self._session:
            self._session.close()
            self._session = None
