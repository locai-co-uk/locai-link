# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Sink that routes pipeline data to the agent's command handler."""

import collections
import logging
from collections import deque
from typing import Any

from typing_extensions import override

from link.components.registry import ComponentRegistry, Sink

logger = logging.getLogger(__name__)


@ComponentRegistry.register("command")
class AgentCommand(Sink):
    """
    Routes pipeline data directly to the Agent Runtime's command handler.

    Bounded `id` dedup deque rejects duplicates silently. The same command can
    reach this sink twice when the runtime drains a Firestore backlog over HTTP
    and a live Zenoh inbox sample for the same id arrives in parallel - the
    dedup window lets `mark_seen()` pre-populate from the HTTP response so the
    live sample is recognised and dropped.
    """

    def __init__(self, callback, dedup_window: int = 2000):
        """Initialises the AgentCommand sink.

        Args:
            callback (Callable): The function to call with command data.
            dedup_window (int): Maximum recent command ids to remember.
        """
        self.callback = callback
        self._seen: deque[str] = collections.deque(maxlen=dedup_window)

    @override
    def __call__(self, data: dict[str, Any] | list[dict[str, Any]]) -> bool:
        """Dispatches one or more commands to the runtime callback."""
        if not data:
            return True
        cmds = data if isinstance(data, list) else [data]
        return all(self._dispatch(cmd) for cmd in cmds)

    def _dispatch(self, cmd: dict[str, Any]) -> bool:
        cmd_id = cmd.get("id")
        # Truthy check on both branches — matches mark_seen() so an
        # empty-string id (which isn't a useful dedup key anyway)
        # doesn't get treated differently by the two entry points.
        if cmd_id and cmd_id in self._seen:
            return True  # duplicate, silently ack
        try:
            self.callback(cmd)
        except Exception as e:
            logger.error(f"Command execution failed: {e}")
            # Don't record cmd_id in _seen — a retry should be allowed to try again.
            return False
        if cmd_id:
            self._seen.append(cmd_id)
        return True

    def mark_seen(self, cmd_id: str) -> None:
        """Pre-populate dedup before the consumer starts (used by runtime reconcile)."""
        if cmd_id:
            self._seen.append(cmd_id)
