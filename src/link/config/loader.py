# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from link.config.models import AgentConfig

logger = logging.getLogger(__name__)

# Regex to find patterns like ${identity.device_id} or ${transport.type}
PLACEHOLDER_PATTERN = re.compile(r"\$\{([a-zA-Z0-9_.]+)\}")


def load_config(path: Path) -> AgentConfig:
    """Loads a JSON configuration file, dynamically resolves ${path.to.key} placeholders, and validates the schema.

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

        # We pass raw_data as 'root' so we can lookup absolute paths from anywhere
        resolved_data = _resolve_recursively(raw_data, root_data=raw_data)

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


def _resolve_recursively(item: Any, root_data: dict) -> Any:
    """Traverses the configuration tree. If a string is found, checks for ${placeholders}.

    Args:
        item (Any): The configuration item (dict, list, or string).
        root_data (dict): The root configuration dictionary for resolution.

    Returns:
        Any: The resolved configuration item.
    """
    if isinstance(item, dict):
        return {k: _resolve_recursively(v, root_data) for k, v in item.items()}

    elif isinstance(item, list):
        return [_resolve_recursively(i, root_data) for i in item]

    elif isinstance(item, str):
        return _substitute_string(item, root_data)

    return item


def _substitute_string(text: str, root_data: dict) -> str:
    """Replaces ${path.to.key} with the actual value from root_data.

    Args:
        text (str): The text containing placeholders.
        root_data (dict): The root configuration dictionary.

    Returns:
        str: The string with placeholders replaced.
    """
    if "${" not in text:
        return text

    def replacer(match):
        path_str = match.group(1)  # e.g., "identity.device_id"
        keys = path_str.split(".")

        # Traverse the root object
        current = root_data
        try:
            for key in keys:
                if isinstance(current, dict):
                    current = current.get(key)
                else:
                    # We hit a dead end (e.g., trying to access property of a string)
                    raise KeyError(key)

                if current is None:
                    raise KeyError(key)

            return str(current)

        except KeyError:
            logger.warning(f"Config placeholder unresolved: ${{{path_str}}} (Path not found)")
            # Return original string (e.g. "${identity.missing}") so it's obvious in logs
            return match.group(0)

    return PLACEHOLDER_PATTERN.sub(replacer, text)
