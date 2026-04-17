# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import json

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

    config = AgentConfig(version=2.1, identity={"device_id": "new-dev", "device_name": "test"}, pipelines=[])
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
    assert result["identity"]["device_id"] == "20260101_120000"


def test_set_pipeline_status(tmp_path):
    """Sets pipeline active flag and persists."""
    mgr = StateManager()
    mgr.STATE_DIR = tmp_path

    config = AgentConfig(
        version=2.1,
        identity={"device_id": "d"},
        pipelines=[{"id": "p1", "source": {"type": "clock_tick"}}],
    )
    mgr.bootstrap(config)

    mgr.set_pipeline_status("p1", True)

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
