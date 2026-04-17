# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import json
import logging
import queue
import time
from typing import Any

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
        """Initialise the publisher.

        Args:
            session (Any): The Zenoh session.
            topic (str): The publication topic.
            storage_type (str): Type of storage backend ('zenoh' or 'sqlite').
            storage_config (dict | None): Configuration for the storage backend.
        """
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

    def __call__(self, data: Any) -> bool | None:
        """Publish data to Zenoh.

        Args:
            data (Any): The data to publish.

        Returns:
            bool | None: True if published, None if data is None.
        """
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
    """
    Queue-based Zenoh subscriber.
    Acts as a Pipeline Source - messages are buffered and returned via __call__().
    """

    def __init__(self, session, topic):
        """Initialise ZenohListener.

        Args:
            session (Any): The Zenoh session.
            topic (str): The subscription topic.
        """
        self.session = session
        self.topic = topic
        self._queue: queue.Queue = queue.Queue()
        self._subscriber = None

        self.start()

    def start(self):
        """Start the listener subscribed to the topic."""
        logger.info(f"Subscribing to: {self.topic}")
        self._subscriber = self.session.declare_subscriber(self.topic, self._on_message)

    def _on_message(self, sample):
        """Callback from Zenoh - push to internal queue.

        Args:
            sample (Any): The Zenoh sample/message.
        """
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

    def __call__(self) -> Any | None:
        """Pull next message from queue (non-blocking).

        Returns:
            Any | None: The message data or None if queue is empty.
        """
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def stop(self):
        """Stop the subscriber and undeclare the subscription."""
        if self._subscriber:
            self._subscriber.undeclare()
            # self._subscriber = None
