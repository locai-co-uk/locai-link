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
    """
    Manages the Zenoh session using direct configuration arguments.
    """

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
