# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""JSON config loader with self-referential template placeholder resolution."""

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from link.config.models import AgentConfig
from link.config.templating import resolve_templates

logger = logging.getLogger(__name__)


def load_config(path: Path) -> AgentConfig:
    """Loads a JSON config file, resolves `${path.to.key}` placeholders against itself, and validates.

    Placeholders in the file resolve self-referentially — e.g. a sink URL can
    reference `${identity.device_id}` defined earlier in the same file.

    Args:
        path (Path): Path to the configuration file.

    Returns:
        AgentConfig: The validated AgentConfig object.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If JSON is invalid or schema validation fails.
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)

        # Self-referential resolution: placeholders look up values from the same dict.
        resolved_data = resolve_templates(raw_data, context=raw_data)

        return AgentConfig(**resolved_data)

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON syntax in {path}: {e}")
        raise ValueError(f"Invalid JSON syntax: {e}") from e
    except ValidationError as e:
        logger.error(f"Schema Validation Failed for {path}:\n{e}")
        raise ValueError(f"Schema validation failed: {e}") from e
    except Exception as e:
        logger.error(f"Unexpected error loading config: {e}")
        raise ValueError(f"Unexpected error: {e}") from e
