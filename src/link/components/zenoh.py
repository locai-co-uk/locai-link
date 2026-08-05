# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Zenoh pub/sub components: publisher (sink) and listener (source)."""

import json
import logging
import queue
import time
from typing import Any

from typing_extensions import override

from link.adapters.persistence import SQLiteStorageBackend, StorageBackend, ZenohStorageBackend
from link.components.registry import ComponentRegistry, Sink, Source

logger = logging.getLogger(__name__)


@ComponentRegistry.register("zenoh_pub")
class ZenohPublisher(Sink):
    """Publishes data to Zenoh."""

    def __init__(
        self,
        session,
        topic,
        storage_type="zenoh",
        storage_config=None,
    ):
        """Initialise the publisher (storage_type: 'zenoh' or 'sqlite')."""
        self.topic = topic

        # Strategy Factory
        self.backend: StorageBackend
        if storage_type == "zenoh":
            self.backend = ZenohStorageBackend(session)
        elif storage_type == "sqlite":
            args = storage_config if storage_config else {}
            self.backend = SQLiteStorageBackend(**args)
        else:
            logger.warning(f"Warning: Unknown storage_type '{storage_type}'. Defaulting to Zenoh.")
            self.backend = ZenohStorageBackend(session)

    @override
    def __call__(self, data: Any) -> bool | None:
        """Publish data to Zenoh; returns True, or None if data is empty."""
        if not data:
            return None

        # 1. Determine Unique Topic Key
        unique_id = time.time_ns()
        key = f"{self.topic}/{unique_id}"

        # 2. Serialise
        if isinstance(data, dict):
            payload = json.dumps(data)
        else:
            payload = str(data)

        # 3. Send/Store via Backend
        self.backend.save(key, payload)

        return True


@ComponentRegistry.register("zenoh_sub")
class ZenohListener(Source):
    """Queue-based Zenoh subscriber (pipeline source); buffers messages and returns them via __call__()."""

    def __init__(self, session, topic):
        """Initialise ZenohListener on the given session and topic."""
        self.session = session
        self.topic = topic
        self._queue: queue.Queue[Any] = queue.Queue()
        self._subscriber = None

        self.start()

    def start(self):
        """Start the listener subscribed to the topic."""
        logger.info(f"Subscribing to: {self.topic}")
        self._subscriber = self.session.declare_subscriber(self.topic, self._on_message)

    def _on_message(self, sample):
        """Zenoh callback: decode the sample and push it onto the internal queue."""
        try:
            payload = sample.payload.to_bytes().decode("utf-8")
        except AttributeError:
            payload = bytes(sample.payload).decode("utf-8")

        # Try to parse as JSON, otherwise return as string
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = payload

        self._queue.put(data)

    @override
    def __call__(self) -> Any | None:
        """Pull the next queued message, or None if empty (non-blocking)."""
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def stop(self):
        """Stop the subscriber and undeclare the subscription."""
        if self._subscriber:
            self._subscriber.undeclare()
            # self._subscriber = None
