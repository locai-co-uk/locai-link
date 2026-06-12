# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import json
import os
import stat

import pytest

from link.app.state import StateManager
from link.config.models import AgentConfig


def test_load_valid_state(tmp_path):
    """Loads a valid v2.1 session file."""
    state = {"version": 2.1, "identity": {"device_id": "d1"}, "pipelines": []}
    session_file = tmp_path / "session_20260101_120000.json"
    session_file.write_text(json.dumps(state))

    mgr = StateManager()
    mgr.STATE_DIR = tmp_path

    result = mgr.load_state()
    assert result is not None
    assert result["identity"]["device_id"] == "d1"


def test_reject_incompatible_version(tmp_path):
    """Rejects state files with wrong version."""
    state = {"version": 1.0, "identity": {"device_id": "d1"}, "pipelines": []}
    session_file = tmp_path / "session_20260101_120000.json"
    session_file.write_text(json.dumps(state))

    mgr = StateManager()
    mgr.STATE_DIR = tmp_path

    result = mgr.load_state()
    assert result is None


def test_load_missing_session(tmp_path):
    """Returns None when no session files exist."""
    mgr = StateManager()
    mgr.STATE_DIR = tmp_path

    assert mgr.load_state() is None


def test_load_explicit_path(tmp_path):
    """Loads a specific session file by path."""
    state = {"version": 2.1, "identity": {"device_id": "explicit"}, "pipelines": []}
    target = tmp_path / "session_custom.json"
    target.write_text(json.dumps(state))

    mgr = StateManager()
    mgr.STATE_DIR = tmp_path

    result = mgr.load_state(explicit_path=target)
    assert result is not None
    assert result["identity"]["device_id"] == "explicit"


def test_load_explicit_path_missing(tmp_path):
    """Returns None for a non-existent explicit path."""
    mgr = StateManager()
    mgr.STATE_DIR = tmp_path

    result = mgr.load_state(explicit_path=tmp_path / "does_not_exist.json")
    assert result is None


def test_bootstrap_creates_session_file(tmp_path):
    """Bootstrap creates a timestamped session file."""
    mgr = StateManager()
    mgr.STATE_DIR = tmp_path

    config = AgentConfig.model_validate(
        {"version": 2.1, "identity": {"device_id": "new-dev", "device_name": "test"}, "pipelines": []}
    )
    mgr.bootstrap(config)

    assert mgr.current_session_path is not None
    assert mgr.current_session_path.exists()

    data = json.loads(mgr.current_session_path.read_text())
    assert data["identity"]["device_id"] == "new-dev"
    assert data["version"] == 2.1


def test_latest_session_picked(tmp_path):
    """When multiple sessions exist, picks the latest by name sort."""
    for ts in ["20260101_100000", "20260101_120000", "20260101_110000"]:
        f = tmp_path / f"session_{ts}.json"
        f.write_text(json.dumps({"version": 2.1, "identity": {"device_id": ts}, "pipelines": []}))

    mgr = StateManager()
    mgr.STATE_DIR = tmp_path

    result = mgr.load_state()

    assert result is not None

    assert result["identity"]["device_id"] == "20260101_120000"


def test_set_pipeline_status(tmp_path):
    """Sets pipeline active flag and persists."""
    mgr = StateManager()
    mgr.STATE_DIR = tmp_path

    config = AgentConfig.model_validate(
        {
            "version": 2.1,
            "identity": {"device_id": "d"},
            "pipelines": [{"id": "p1", "source": {"type": "clock_tick"}}],
        }
    )
    mgr.bootstrap(config)

    mgr.set_pipeline_status("p1", True)

    assert mgr.current_session_path is not None

    # Re-read from disk
    data = json.loads(mgr.current_session_path.read_text())
    assert data["pipelines"][0]["active"] is True


def test_corrupted_json(tmp_path):
    """Handles corrupted session files gracefully."""
    f = tmp_path / "session_20260101_120000.json"
    f.write_text("{not valid json!!!")

    mgr = StateManager()
    mgr.STATE_DIR = tmp_path

    assert mgr.load_state() is None


def test_update_full_config_preserves_active_flag(tmp_path):
    """Swapping the full config shouldn't reset active flags on pipelines that still exist."""
    mgr = StateManager()
    mgr.STATE_DIR = tmp_path

    original = AgentConfig.model_validate(
        {
            "version": 2.1,
            "identity": {"device_id": "d"},
            "pipelines": [
                {"id": "p1", "source": {"type": "clock_tick"}},
                {"id": "p2", "source": {"type": "clock_tick"}},
            ],
        }
    )
    mgr.bootstrap(original)
    # Simulate p1 being activated at runtime
    mgr.set_pipeline_status("p1", True)

    # New config keeps p1, removes p2, adds p3
    new_cfg = AgentConfig.model_validate(
        {
            "version": 2.1,
            "identity": {"device_id": "d"},
            "pipelines": [
                {"id": "p1", "source": {"type": "clock_tick"}},
                {"id": "p3", "source": {"type": "clock_tick"}},
            ],
        }
    )
    mgr.update_full_config(new_cfg)

    assert mgr.current_session_path is not None

    data = json.loads(mgr.current_session_path.read_text())
    pipelines = {p["id"]: p for p in data["pipelines"]}
    assert pipelines["p1"]["active"] is True, "p1 was running, should still be active"
    assert pipelines["p3"]["active"] is False, "p3 is new, defaults to inactive"
    assert "p2" not in pipelines


def test_update_full_config_replaces_non_pipeline_fields(tmp_path):
    """Full-config swap replaces top-level fields like reporting."""
    mgr = StateManager()
    mgr.STATE_DIR = tmp_path

    original = AgentConfig.model_validate(
        {
            "version": 2.1,
            "identity": {"device_id": "d"},
            "reporting": {"interval": 30},
            "pipelines": [],
        }
    )
    mgr.bootstrap(original)

    new_cfg = AgentConfig.model_validate(
        {
            "version": 2.1,
            "identity": {"device_id": "d"},
            "reporting": {"interval": 120},
            "pipelines": [],
        }
    )
    mgr.update_full_config(new_cfg)

    assert mgr.current_session_path is not None

    data = json.loads(mgr.current_session_path.read_text())
    assert data["reporting"]["interval"] == 120


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes; Windows uses directory ACLs")
def test_session_file_is_owner_only(tmp_path):
    """Session files hold the device api_key and must be created mode 0600."""
    mgr = StateManager()
    mgr.STATE_DIR = tmp_path

    config = AgentConfig.model_validate(
        {"version": 2.1, "identity": {"device_id": "d", "api_key": "secret"}, "pipelines": []}
    )
    mgr.bootstrap(config)

    assert mgr.current_session_path is not None
    mode = stat.S_IMODE(os.stat(mgr.current_session_path).st_mode)
    assert mode == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes; Windows uses directory ACLs")
def test_load_tightens_legacy_session_permissions(tmp_path):
    """Pre-existing world-readable session files are chmodded to 0600 on load."""
    state = {"version": 2.1, "identity": {"device_id": "d1"}, "pipelines": []}
    session_file = tmp_path / "session_20260101_120000.json"
    session_file.write_text(json.dumps(state))
    os.chmod(session_file, 0o644)

    mgr = StateManager()
    mgr.STATE_DIR = tmp_path
    assert mgr.load_state() is not None

    mode = stat.S_IMODE(os.stat(session_file).st_mode)
    assert mode == 0o600
