# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Sink that routes pipeline data to the agent's command handler."""

import logging

from link.components.registry import ComponentRegistry, Sink

logger = logging.getLogger(__name__)


@ComponentRegistry.register("command")
class AgentCommand(Sink):
    """
    Routes pipeline data directly to the Agent Runtime's command handler.
    """

    def __init__(self, callback):
        """Initialises the AgentCommand sink.

        Args:
            callback (Callable): The function to call with command data.
        """
        self.callback = callback

    def __call__(self, data: dict | list[dict]) -> bool:
        """Dispatches one or more commands to the runtime callback."""
        if not data:
            return True
        cmds = data if isinstance(data, list) else [data]
        return all(self._dispatch(cmd) for cmd in cmds)

    def _dispatch(self, cmd: dict) -> bool:
        try:
            self.callback(cmd)
            return True
        except Exception as e:
            logger.error(f"Command execution failed: {e}")
            return False
