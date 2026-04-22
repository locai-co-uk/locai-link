# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""HTTP client adapter with typed errors — distinguishes retryable vs fatal failures."""

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class HttpError(Exception):
    """Raised when an HTTP request fails with actionable context."""

    def __init__(self, status: int | None, reason: str, retryable: bool):
        """Attach the HTTP status code, reason text, and retryability hint."""
        self.status = status
        self.reason = reason
        self.retryable = retryable
        super().__init__(f"HTTP {status or 'N/A'}: {reason}")


class HttpClient:
    """A robust HTTP client adapter."""

    def __init__(
        self, base_url: str | None = None, default_headers: dict[str, str] | None = None, timeout: float = 5.0
    ):
        """Initialises the HttpClient.

        Args:
            base_url (str | None): The base URL for the client.
            default_headers (dict[str, str] | None): Default headers to include in every request.
            timeout (float): Request timeout in seconds.
        """
        # Clean the base_url but handle None gracefully
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.timeout = timeout
        self.session = requests.Session()

        if default_headers:
            self.session.headers.update(default_headers)

    def get(self, endpoint: str = "", params: dict[str, Any] | None = None) -> Any | None:
        """Performs a GET request.

        Args:
            endpoint (str): The API endpoint (relative to base_url).
            params (dict[str, Any] | None): Query parameters.

        Returns:
            Any | None: The JSON response data or None if the request failed.

        Raises:
            HttpError: On non-retryable failures (auth errors, not found, etc.).
        """
        url = self._build_url(endpoint)
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.Timeout:
            # Self-healing: next poll retries. Keep at debug to avoid log spam on flaky networks.
            logger.debug(f"HTTP GET Timeout ({url})")
            return None
        except requests.ConnectionError:
            logger.warning(f"HTTP GET Connection Failed ({url})")
            return None
        except requests.HTTPError as e:
            status = e.response.status_code
            if status >= 500:
                logger.warning(f"HTTP GET Server Error ({url}): {status}")
                return None
            raise HttpError(status, e.response.text, retryable=False) from e
        except requests.JSONDecodeError:
            logger.warning(f"HTTP GET Invalid JSON ({url})")
            return None

    def post(self, endpoint: str = "", json_data: Any = None) -> bool:
        """Performs a POST request.

        Args:
            endpoint (str): The API endpoint (relative to base_url).
            json_data (Any): The JSON payload to send.

        Returns:
            bool: True if the request was successful, False otherwise.

        Raises:
            HttpError: On non-retryable failures (auth errors, bad request, etc.).
        """
        url = self._build_url(endpoint)
        try:
            resp = self.session.post(url, json=json_data, timeout=self.timeout)
            resp.raise_for_status()
            return True
        except requests.Timeout:
            logger.warning(f"HTTP POST Timeout ({url})")
            return False
        except requests.ConnectionError:
            logger.warning(f"HTTP POST Connection Failed ({url})")
            return False
        except requests.HTTPError as e:
            status = e.response.status_code
            if status >= 500:
                logger.warning(f"HTTP POST Server Error ({url}): {status}")
                return False
            raise HttpError(status, e.response.text, retryable=False) from e

    def close(self):
        """Closes the underlying session."""
        self.session.close()

    def _build_url(self, endpoint: str) -> str:
        """Intelligently joins base_url and endpoint without forcing slashes.

        Args:
            endpoint (str): The target endpoint.

        Returns:
            str: The full URL.
        """
        # 1. Absolute Overrides (e.g. get("https://google.com"))
        if endpoint.startswith("http"):
            return endpoint

        # 2. No Endpoint provided? Return base exactly as is.
        if not endpoint:
            return self.base_url

        # 3. Join with slash
        # If we have a base, join them. If no base, return endpoint (relative path).
        if self.base_url:
            return f"{self.base_url}/{endpoint.lstrip('/')}"
        return endpoint
