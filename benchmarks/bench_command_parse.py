# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Per-command validation hot path: template resolution + schema parse."""

from link.config.commands import parse_command
from link.config.templating import resolve_templates

_CONTEXT = {
    "identity": {
        "device_id": "dev-123",
        "device_name": "edge-01",
        "api_key": "k" * 40,
        "api_url": "https://api.example.com",
    },
    "api_url": "https://api.example.com",
}

_RAW_COMMAND = {
    "id": "cmd-1",
    "type": "START_SERVING",
    "pipeline_id": "pipe-a",
    "port": 8100,
    "host": "127.0.0.1",
    "model_display_name": "${identity.device_name}-model",
}


def test_resolve_and_parse(benchmark):
    def _run():
        return parse_command(resolve_templates(_RAW_COMMAND, _CONTEXT))

    cmd = benchmark(_run)
    assert cmd.type == "START_SERVING"
