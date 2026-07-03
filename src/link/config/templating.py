# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Template placeholder resolution for backend-provided configs.

The backend delivers `AgentConfig` templates at registration time with
placeholders like `${identity.device_id}` and `${identity.api_key}`. Values
known only on the client side (the `api_url` the agent was launched with, the
`api_key` returned in the same response) are substituted by `resolve_templates`.

Unknown placeholders are preserved verbatim — this lets runtime placeholders
like `{cid}` / `{mid}` pass through untouched (they use a different syntax and
are substituted by handlers at emit time).
"""

import re
from typing import Any

_TEMPLATE_RE = re.compile(r"\$\{([^}]+)\}")


def resolve_templates(obj: Any, context: dict[str, Any]) -> Any:
    """Recursively substitute `${path.to.key}` placeholders using dotted lookups.

    Args:
        obj: Any JSON-like value. `dict`, `list`, `str` are walked; other types
            are returned unchanged.
        context: Nested dict mapping placeholder namespaces to values.
            e.g. `{"identity": {"device_id": "dev_abc", ...}}`.

    Returns:
        The same structure with resolvable placeholders substituted. Unknown
        placeholders are left as literal strings.
    """
    if isinstance(obj, str):
        return _TEMPLATE_RE.sub(lambda m: _lookup(context, m.group(1)), obj)
    if isinstance(obj, dict):
        return {k: resolve_templates(v, context) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_templates(v, context) for v in obj]
    return obj


def _lookup(context: dict[str, Any], path: str) -> str:
    """Resolve `a.b.c` from `context['a']['b']['c']` — or return the literal placeholder."""
    node: Any = context
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return f"${{{path}}}"
        node = node[part]
    return str(node)
