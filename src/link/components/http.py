# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""HTTP pipeline components — polling source and publishing sink."""

import logging
import time
from typing import Any

from link.adapters.http_client import HttpClient, HttpError
from link.components.registry import ComponentRegistry, Sink, Source

logger = logging.getLogger(__name__)


@ComponentRegistry.register("http_poll")
class HttpPoller(Source):
    """Polls a REST API at a fixed interval with Auth support."""

    def __init__(self, url: str, api_key: str | None = None, interval: float = 1.0, **kwargs):
        """Initialises the HttpPoller.

        Args:
            url (str): The target URL.
            api_key (str | None): Optional API key for Bearer auth.
            interval (float): Polling interval in seconds.
            kwargs (Any): Additional arguments.
        """
        # 1. Prepare Headers
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 2. Init Client with Headers
        self.client = HttpClient(base_url=url, default_headers=headers)
        self.interval = float(interval)
        self.last_poll = 0.0

    def __call__(self) -> Any | None:
        """Polls the configured URL.

        Returns:
            Any | None: The response data if polling occurred, else None.

        Raises:
            HttpError: Propagated for non-retryable failures (e.g. 401/403).
        """
        now = time.time()
        if now - self.last_poll < self.interval:
            return None

        self.last_poll = now
        try:
            return self.client.get()
        except HttpError as e:
            # 401/403 on the command channel = creds were revoked or rotated.
            # Surface as authentication so it's findable in the backend UI.
            category = "authentication" if e.status in (401, 403) else "execution"
            logger.error(f"Command poll rejected ({e.status}): {e.reason}", extra={"category": category})
            raise


@ComponentRegistry.register("http_post")
class HttpPublisher(Sink):
    """Posts data to a REST API with Auth support."""

    def __init__(self, url: str, api_key: str | None = None, timeout: int = 10, **kwargs):
        """Initialises the HttpPublisher.

        Args:
            url (str): The target URL.
            api_key (str | None): Optional API key for Bearer auth.
            timeout (int): Request timeout in seconds.
            kwargs (Any): Additional arguments.
        """
        # 1. Prepare Headers
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 2. Init Client with Headers
        self.client = HttpClient(base_url=url, default_headers=headers, timeout=timeout)

    def __call__(self, payload: Any) -> bool | None:
        """Posts the payload to the configured URL.

        Args:
            payload (Any): The data to post.

        Returns:
            bool | None: True if successful, False if failed, None if no payload.

        Raises:
            HttpError: Propagated for non-retryable failures (e.g. 401/403).
        """
        if not payload:
            return None

        try:
            return self.client.post(json_data=payload)
        except HttpError as e:
            logger.error(f"HTTP publish rejected ({e.status}): {e.reason}")
            raise
