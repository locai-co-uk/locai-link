# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Storage backends: Zenoh (network pub/sub) and SQLite (local buffering)."""

import json
import logging
import sqlite3
from abc import ABC, abstractmethod
from typing import Any

from typing_extensions import override

logger = logging.getLogger(__name__)


class StorageBackend(ABC):
    """Abstract interface for data persistence."""

    @abstractmethod
    def save(self, key: str, data: Any) -> None:
        """Saves data to the storage backend.

        Args:
            key (str): The key to identify the data.
            data (Any): The data to be saved.
        """
        pass

    def close(self) -> None:
        """Closes the storage connection. Default: no-op — override if needed."""
        pass


class ZenohStorageBackend(StorageBackend):
    """Publishes data into the Zenoh network.

    A network adapter exposed through the Storage interface so the Publisher treats it like any backend.
    """

    def __init__(self, session):
        """Initialises the ZenohStorageBackend.

        Args:
            session (Any): The Zenoh session object.
        """
        self.session = session

    @override
    def save(self, key: str, data: Any) -> None:
        """Saves data to Zenoh.

        Args:
            key (str): The key under which to publish the data.
            data (Any): The data payload.
        """
        if not self.session:
            logger.warning("Zenoh put dropped (no active session): key=%s", key)
            return

        payload = json.dumps(data) if isinstance(data, (dict, list)) else str(data)
        try:
            self.session.put(key, payload)
        except Exception as e:
            logger.warning(f"Zenoh Put Failed: {e}")

    # close() inherits from base: no-op. Session lifetime is managed by
    # InfrastructureManager, not by this backend.


class SQLiteStorageBackend(StorageBackend):
    """Persists data to a local SQLite database (e.g. for offline buffering)."""

    def __init__(self, db_path: str = "buffer.db"):
        """Initialises the SQLiteStorageBackend.

        Args:
            db_path (str): Path to the SQLite database file. Defaults to "buffer.db".
        """
        self.db_path = db_path
        self._conn = None
        self._setup_db()

    def _setup_db(self):
        """Sets up the SQLite database table if it doesn't exist."""
        try:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS buffer (id INTEGER PRIMARY KEY, key TEXT, data TEXT, timestamp REAL)"
            )
            self._conn.commit()
        except Exception as e:
            logger.error(f"Failed to init SQLite buffer: {e}")

    @override
    def save(self, key: str, data: Any) -> None:
        """Saves data to the local SQLite database.

        Args:
            key (str): The storage key.
            data (Any): The data to persist.
        """
        if not self._conn:
            return

        payload = json.dumps(data) if isinstance(data, (dict, list)) else str(data)
        try:
            self._conn.execute(
                "INSERT INTO buffer (key, data, timestamp) VALUES (?, ?, strftime('%J','now'))", (key, payload)
            )
            self._conn.commit()
        except Exception as e:
            logger.error(f"SQLite Save Failed: {e}")

    @override
    def close(self) -> None:
        """Closes the SQLite connection."""
        if self._conn:
            self._conn.close()
