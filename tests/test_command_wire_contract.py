# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Wire-contract test: the agent must accept every command it can receive.

The fixtures in ``tests/fixtures/wire/*.json`` are the frozen, on-the-wire form
of every command type the agent consumes. Each must validate via
`parse_command` (after resolving ``${identity.*}``, as the runtime does) into
the right typed command.

``CONTRACT_SHA256`` pins the fixture set: an accidental edit fails this test
instead of silently weakening it. Change the fixtures only deliberately, then
update the hash to match. `START_MODEL` is excluded; it is issued internally,
never received over the wire.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from link.config.commands import (
    CancelDeployCommand,
    DeployModelCommand,
    StartModelInferenceCommand,
    StartServingCommand,
    StatusCommand,
    StopModelInferenceCommand,
    StopServingCommand,
    UninstallModelCommand,
    UpdateAgentCommand,
    UpdateAgentConfigCommand,
    UpdatePipelineCommand,
    parse_command,
)
from link.config.templating import resolve_templates

# Fingerprint of the frozen fixture set (see test_contract_fingerprint_matches).
CONTRACT_SHA256 = "425ce1d54b81939f310d6d0806c05fdbc2c7d96c5e0531dc38b6ed06e26fd730"

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "wire"
_FIXTURES = sorted(FIXTURES_DIR.glob("*.json"))

# Every over-the-wire command type, mapped to the typed command it parses into.
EXPECTED_CLASS = {
    "DEPLOY_MODEL": DeployModelCommand,
    "CANCEL_DEPLOY": CancelDeployCommand,
    "START_MODEL_INFERENCE": StartModelInferenceCommand,
    "STOP_MODEL_INFERENCE": StopModelInferenceCommand,
    "START_SERVING": StartServingCommand,
    "STOP_SERVING": StopServingCommand,
    "UNINSTALL_MODEL": UninstallModelCommand,
    "UPDATE_PIPELINE": UpdatePipelineCommand,
    "STATUS": StatusCommand,
    "UPDATE_AGENT": UpdateAgentCommand,
    "UPDATE_AGENT_CONFIG": UpdateAgentConfigCommand,
}

# Identity context the runtime uses to resolve ${identity.*} before validation.
_IDENTITY_CONTEXT = {
    "identity": {
        "device_id": "dev-test-01",
        "device_name": "test-device",
        "api_key": "test-key",
        "api_url": "https://api.test",
    },
    "api_url": "https://api.test",
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _fingerprint(fixtures_dir: Path) -> str:
    """Deterministic hash of the fixture set.

    Re-parses each file so cosmetic whitespace/key-order in the raw JSON does
    not affect the hash, only semantic content (and the filename) does.
    """
    items: list[list[Any]] = []
    for path in sorted(fixtures_dir.glob("*.json")):
        items.append([path.name, _load(path)])
    blob = json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def test_fixtures_present():
    """Guard against an empty/missing fixtures directory silently passing."""
    assert _FIXTURES, f"No wire fixtures found under {FIXTURES_DIR}"


@pytest.mark.parametrize("path", _FIXTURES, ids=[p.stem for p in _FIXTURES])
def test_parse_command_accepts_fixture(path: Path):
    """Each fixture resolves and validates into the expected typed command."""
    raw = _load(path)
    resolved = resolve_templates(raw, _IDENTITY_CONTEXT)
    cmd = parse_command(resolved)

    expected = EXPECTED_CLASS[raw["type"]]
    assert isinstance(cmd, expected), f"{path.name}: expected {expected.__name__}, got {type(cmd).__name__}"
    assert cmd.id == raw["id"]


def test_every_command_type_has_a_fixture():
    """Every over-the-wire command type must be represented by a fixture."""
    covered = {_load(p)["type"] for p in _FIXTURES}
    assert covered == set(EXPECTED_CLASS), (
        f"Missing fixtures for: {set(EXPECTED_CLASS) - covered}; unexpected types: {covered - set(EXPECTED_CLASS)}"
    )


def test_identity_placeholder_is_resolved():
    """The ${identity.device_id} in a deploy sink topic is substituted before parse."""
    cmd = parse_command(resolve_templates(_load(FIXTURES_DIR / "deploy_model.json"), _IDENTITY_CONTEXT))
    assert isinstance(cmd, DeployModelCommand)
    assert cmd.config.sink is not None
    assert cmd.config.sink.args["topic"] == "locai/devices/dev-test-01/models/llama-3-8b/results"
    assert "${" not in cmd.config.sink.args["topic"]


def test_contract_fingerprint_matches():
    """Fixtures are unchanged since the contract was last frozen.

    If this fails and the change was intentional, update CONTRACT_SHA256 to the
    printed value.
    """
    actual = _fingerprint(FIXTURES_DIR)
    assert actual == CONTRACT_SHA256, (
        f"Wire fixtures changed. New fingerprint: {actual}\nIf intentional, update CONTRACT_SHA256 to match."
    )
