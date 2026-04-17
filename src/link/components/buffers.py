# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import logging
from typing import Any

from link.adapters.persistence import SQLiteStorageBackend
from link.components.registry import Sink

logger = logging.getLogger(__name__)


class LocalBuffer(Sink):
    """
    TODO: Making the sink a list makes this feasible. Right now only zenoh is handling
    this and its implicit, make all buffering explicit.
    A Pipeline Sink that saves data to a local SQLite database.
    Useful for:
      - Data logging (Flight Recorder)
      - Offline buffering
      - Debugging without a network
    """

    def __init__(self, db_path: str = "local_buffer.db"):
        """Initialises the LocalBuffer.

        Args:
            db_path (str): Path to the SQLite database.
        """
        # Initialise the Adapter
        raise NotImplementedError("LocalBuffer is not implemented yet")

        self.adapter = SQLiteStorageBackend(db_path)

    def __call__(self, data: Any) -> dict[str, str] | None:
        """Pipeline Sink Interface.

        Args:
            data (Any): The data to buffer.

        Returns:
            dict | None: Status dictionary if buffered, else None.
        """
        if data is None:
            return None

        # Generate a key (Timestamp or UUID could be used here)
        # For simplicity, we might just use a generic stream key or extract it from data
        key = "stream_data"
        if isinstance(data, dict) and "id" in data:
            key = data["id"]

        # Delegate to Adapter
        self.adapter.save(key, data)

        return {"status": "buffered", "target": self.adapter.db_path}

    def close(self):
        """Closes the buffer connection."""
        self.adapter.close()
