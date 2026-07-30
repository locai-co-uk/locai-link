# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Hot-reconfiguration — apply a new AgentConfig to a running runtime.

Triggered by the `UPDATE_AGENT_CONFIG` command. The backend sends a full
`AgentConfig` (all placeholders resolved server-side, but we re-resolve
defensively). The runtime validates it, snapshots the current state, swaps
pipelines and handlers in place, and on failure reverts to the snapshot. If
revert also fails the runtime requests a self-restart (execv) — main.py picks
this up and re-execs without running `git pull`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from link.adapters.http_client import HttpClient
from link.config.models import SCHEMA_VERSION, AgentConfig
from link.config.templating import resolve_templates

if TYPE_CHECKING:
    from link.app.runtime import AgentRuntime


logger = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    """Outcome of `apply_agent_config`."""

    ok: bool
    message: str
    scheduled_restart: bool = False


def apply_agent_config(runtime: "AgentRuntime", raw: dict[str, Any]) -> ApplyResult:
    """Validate and apply a new AgentConfig to a running runtime.

    Args:
        runtime: The live `AgentRuntime` instance.
        raw: The new config dict (from the backend command payload).

    Returns:
        `ApplyResult` describing success, failure reason, or scheduled restart.
    """
    # 1. Resolve identity placeholders defensively.
    #    The backend should resolve these server-side, but if it sends a raw
    #    template we can still apply it by filling in our known identity.
    cur_identity = runtime.agent_config.identity
    context = {
        "identity": {
            "device_id": cur_identity.device_id,
            "device_name": cur_identity.device_name,
            "api_key": cur_identity.api_key,
            "api_url": cur_identity.api_url,
        },
        "api_url": cur_identity.api_url,
    }
    resolved = resolve_templates(raw, context)

    # 2. Validate schema version + shape.
    if resolved.get("version") != SCHEMA_VERSION:
        return ApplyResult(False, f"Unsupported version {resolved.get('version')!r}")

    try:
        new_cfg = AgentConfig(**resolved)
    except Exception as e:
        return ApplyResult(False, f"Invalid config: {e}")

    # 3. Identity drift guard — defence in depth.
    #    Only device_id is checked here. api_url and api_key are intentionally
    #    excluded: api_url is controlled by the --api-url CLI arg and the
    #    backend may store a different value (e.g. prod URL) in the identity
    #    section than what the agent resolved at startup; api_key immutability
    #    is already enforced by the backend before the command is queued.
    new_identity = new_cfg.identity
    if new_identity.device_id != cur_identity.device_id:
        return ApplyResult(
            False,
            f"Identity drift — device_id mismatch: expected {cur_identity.device_id!r}, got {new_identity.device_id!r}",
        )

    # 4. Snapshot current state for revert.
    snapshot_cfg = runtime.agent_config.model_copy(deep=True)
    snapshot_state = (
        runtime.state_manager.snapshot() if runtime.state_manager is not None else None
    )

    # 5. Apply — hot-swap pipelines + handlers, then persist the new config.
    try:
        runtime.apply_config(new_cfg, previous_cfg=snapshot_cfg)
        if runtime.state_manager is not None:
            runtime.state_manager.update_full_config(new_cfg)
        identity = new_cfg.identity
        try:
            client = HttpClient(
                base_url=identity.api_url,
                default_headers={"Authorization": f"Bearer {identity.api_key}"},
                timeout=10.0,
            )
            client.post(
                f"agent/{identity.device_id}/update_applied_agent_config",
                json_data={"config": new_cfg.model_dump()},
            )
            client.close()
        except Exception as e:
            logger.warning(f"Could not report applied config to backend: {e}")
        return ApplyResult(True, f"Applied - {len(new_cfg.pipelines)} pipeline(s)")

    except Exception as apply_err:
        logger.error(f"apply_agent_config failed: {apply_err}", exc_info=True)

        # 6. Revert to snapshot — same hot-swap in reverse, then restore state.
        try:
            runtime.apply_config(snapshot_cfg, previous_cfg=new_cfg)
            if runtime.state_manager is not None:
                runtime.state_manager.restore(snapshot_state)
            return ApplyResult(False, f"Reverted after failure: {apply_err}")

        except Exception as revert_err:
            # Nuclear: persist the new config and request a full process restart.
            logger.critical(f"Revert failed: {revert_err} — scheduling config restart")
            if runtime.state_manager is not None:
                try:
                    runtime.state_manager.update_full_config(new_cfg)
                except Exception:
                    pass
            runtime.config_restart_requested = True
            runtime.running = False
            runtime.shutdown_event.set()
            return ApplyResult(
                False,
                f"Apply + revert failed — restarting ({revert_err})",
                scheduled_restart=True,
            )
