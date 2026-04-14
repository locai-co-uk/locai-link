# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

from link.serving.llm_server import LLMServer
from link.serving.whisper_server import WhisperServer

# Canonical alias used across agent.py and tests
ModelServer = LLMServer

__all__ = ["LLMServer", "WhisperServer", "ModelServer"]
