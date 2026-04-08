# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import json

from link.utils import is_process_running, load_json_config, stop_process_tree

# -- load_json_config ---------------------------------------------------------


class TestLoadJsonConfig:
    """Tests for load_json_config."""

    def test_valid_json(self, tmp_path):
        """Valid JSON file returns a dict."""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"key": "value"}))
        result = load_json_config(cfg)
        assert result == {"key": "value"}

    def test_missing_file(self, tmp_path):
        """Non-existent file returns None."""
        result = load_json_config(tmp_path / "does_not_exist.json")
        assert result is None

    def test_corrupt_json(self, tmp_path):
        """Malformed JSON returns None without raising."""
        cfg = tmp_path / "bad.json"
        cfg.write_text("{not valid json!!")
        result = load_json_config(cfg)
        assert result is None

    def test_empty_file(self, tmp_path):
        """Empty file returns None (JSONDecodeError)."""
        cfg = tmp_path / "empty.json"
        cfg.write_text("")
        result = load_json_config(cfg)
        assert result is None

    def test_empty_object(self, tmp_path):
        """Empty JSON object returns an empty dict."""
        cfg = tmp_path / "empty_obj.json"
        cfg.write_text("{}")
        result = load_json_config(cfg)
        assert result == {}


# -- is_process_running -------------------------------------------------------


class TestIsProcessRunning:
    """Tests for is_process_running."""

    def test_no_pid_file(self, tmp_path):
        """Returns False if PID file doesn't exist."""
        assert is_process_running(tmp_path / "missing.pid") is False

    def test_valid_pid_running(self, tmp_path, mocker):
        """Returns True if PID file exists and process is alive."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")
        mocker.patch("link.utils.psutil.pid_exists", return_value=True)

        assert is_process_running(pid_file) is True
        assert pid_file.exists(), "PID file should not be deleted for a running process"

    def test_stale_pid(self, tmp_path, mocker):
        """Returns False and cleans up if process is no longer running."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("99999")
        mocker.patch("link.utils.psutil.pid_exists", return_value=False)

        assert is_process_running(pid_file) is False
        assert not pid_file.exists(), "Stale PID file should be removed"

    def test_malformed_pid(self, tmp_path):
        """Returns False and cleans up if PID file contains non-integer."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("not_a_number")

        assert is_process_running(pid_file) is False
        assert not pid_file.exists(), "Malformed PID file should be removed"

    def test_empty_pid_file(self, tmp_path):
        """Returns False and cleans up if PID file is empty."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("")

        assert is_process_running(pid_file) is False
        assert not pid_file.exists()


# -- stop_process_tree --------------------------------------------------------


class TestStopProcessTree:
    """Tests for stop_process_tree."""

    def test_no_pid_file(self, tmp_path, capsys):
        """Does nothing if PID file is missing."""
        stop_process_tree(tmp_path / "missing.pid", "Test")
        assert "No Test PID file found" in capsys.readouterr().out

    def test_process_not_running(self, tmp_path, mocker, capsys):
        """Cleans up PID file if process is already dead."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("99999")
        mocker.patch("link.utils.psutil.pid_exists", return_value=False)

        stop_process_tree(pid_file, "Test")

        assert "already stopped" in capsys.readouterr().out
        assert not pid_file.exists(), "PID file should be cleaned up"

    def test_graceful_terminate(self, tmp_path, mocker):
        """Terminates parent and children, then waits."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("1000")
        mocker.patch("link.utils.psutil.pid_exists", return_value=True)

        mock_child = mocker.MagicMock()
        mock_parent = mocker.MagicMock()
        mock_parent.children.return_value = [mock_child]
        mocker.patch("link.utils.psutil.Process", return_value=mock_parent)

        # All processes exit gracefully — none left alive
        mocker.patch("link.utils.psutil.wait_procs", return_value=([], []))

        stop_process_tree(pid_file, "Test")

        mock_child.terminate.assert_called_once()
        mock_parent.terminate.assert_called_once()
        mock_child.kill.assert_not_called()
        assert not pid_file.exists()

    def test_force_kill_on_timeout(self, tmp_path, mocker):
        """Force-kills processes that don't exit after terminate."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("1000")
        mocker.patch("link.utils.psutil.pid_exists", return_value=True)

        mock_parent = mocker.MagicMock()
        mock_parent.children.return_value = []
        mocker.patch("link.utils.psutil.Process", return_value=mock_parent)

        # Parent is still alive after timeout
        mocker.patch("link.utils.psutil.wait_procs", return_value=([], [mock_parent]))

        stop_process_tree(pid_file, "Test")

        mock_parent.kill.assert_called_once()
        assert not pid_file.exists()

    def test_invalid_pid_file(self, tmp_path, capsys):
        """Handles non-integer PID file gracefully."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("garbage")

        stop_process_tree(pid_file, "Test")

        assert "Invalid" in capsys.readouterr().out
        assert not pid_file.exists()
