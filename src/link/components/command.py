# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

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

    def __call__(self, data: dict) -> bool | None:
        """Processes the command data.

        Args:
            data (dict): The command data (single or list).
        """
        if not data:
            return

        # Determine if data is a list of commands or a single command
        if isinstance(data, list):
            return all(self._dispatch(cmd) for cmd in data)
        else:
            return self._dispatch(data)

    def _dispatch(self, cmd):
        """Dispatches a single command to the callback.

        Args:
            cmd (dict): The command to dispatch.
        """
        try:
            if self.callback:
                self.callback(cmd)
                return True
        except Exception as e:
            logger.error(f"Command execution failed: {e}")
            return False
