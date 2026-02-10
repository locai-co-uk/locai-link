# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import pytest

from link.server import ModelServer


@pytest.fixture
def mock_payload(runtime_config):
    """Generates a valid payload for ModelServer init."""
    return {
        "id": "cmd_test_123",
        "model_id": "test_model",
        "model_name": "test_model.gguf",
        "model_display_name": "test_alias",
        # Ensure these match the runtime_config fixture to pass assertions
        "port": runtime_config["serving"]["default_port"],
        "host": runtime_config["serving"]["default_host"],
        "device_id": "test_device_123",
        "created_at": "2026-01-01T00:00:00Z",
        "status": "pending",
    }


def test_init_success(mocker, mock_paths, device_config, mock_payload):
    """Test successful initialization with valid config and connection."""
    mocker.patch("link.server.load_json_config", return_value=device_config)
    mock_log_client = mocker.patch("link.server.LogClient.get")
    mock_put = mocker.patch("link.server.requests.put")
    mock_put.return_value.status_code = 200

    server = ModelServer(mock_payload)

    assert server.is_valid is True
    assert server.device_id == device_config["device_id"]
    # Check that payload values were set correctly
    assert server.port == mock_payload["port"]
    assert server.model_id == mock_payload["model_id"]

    mock_log_client.return_value.configure.assert_called_with(
        device_config["device_id"], device_config["api_key"], device_config["api_url"]
    )


def test_init_failure_no_config(mocker, mock_paths, mock_payload):
    """Test initialization failing due to missing agent_config.json."""
    mocker.patch("link.server.load_json_config", return_value=None)
    # Patch the specific logger reference imported in server.py
    mock_fail = mocker.patch("link.server.link_logger.fail")

    server = ModelServer(mock_payload)

    assert server.is_valid is False
    mock_fail.assert_called_with("Base config not found.")


def test_start_success(mocker, mock_paths, device_config, runtime_config, mock_payload):
    """Test starting the server successfully."""
    # Mock Config loading sequence
    mocker.patch("link.server.load_json_config", side_effect=[device_config, runtime_config])
    mocker.patch("link.server.LogClient.get")
    mocker.patch("link.server.requests.put").return_value.status_code = 200

    # Mock port check
    if hasattr(ModelServer, "_is_port_in_use"):
        mocker.patch.object(ModelServer, "_is_port_in_use", return_value=False)
    else:
        mocker.patch("link.server.is_process_running", return_value=False)

    # Path.exists mock
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.mkdir")

    # Mock subprocess
    mock_popen = mocker.patch("subprocess.Popen")
    mock_process = mocker.MagicMock()
    mock_process.pid = 12345
    mock_process.poll.return_value = None
    mock_popen.return_value = mock_process

    # Mock file operations
    mocker.patch("pathlib.Path.write_text")
    mocker.patch("builtins.open", mocker.mock_open())

    server = ModelServer(mock_payload)
    server.start()

    # Retrieve all calls made to Popen
    all_calls = mock_popen.call_args_list

    # Filter for the call that actually starts the server
    server_calls = []
    for call in all_calls:
        # call.args[0] is typically the command list
        cmd_args = call.args[0]
        if isinstance(cmd_args, list) and any("server" in str(arg) for arg in cmd_args):
            server_calls.append(call)

    # Assert we found exactly one server start call
    assert len(server_calls) == 1, f"Expected 1 call to start server, found {len(server_calls)}"

    # Get the arguments from that specific call
    call_args, _ = server_calls[0]
    cmd_list = call_args[0]

    # Verify values from runtime_config match the command args
    assert "--port" in cmd_list
    assert str(runtime_config["serving"]["default_port"]) in cmd_list
    assert str(runtime_config["process"]["parameters"]["n_gpu_layers"]) in cmd_list


def test_start_already_running(mocker, mock_paths, device_config, mock_payload):
    """Test start is skipped if process is already running."""
    mocker.patch("link.server.load_json_config", return_value=device_config)
    mocker.patch("link.server.LogClient.get")
    mocker.patch("link.server.requests.put").return_value.status_code = 200

    mocker.patch("link.server.is_process_running", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value="999")

    mock_popen = mocker.patch("subprocess.Popen")

    server = ModelServer(mock_payload)
    server.start()

    mock_popen.assert_not_called()


def test_start_failure_missing_model(mocker, mock_paths, device_config, runtime_config, mock_payload):
    """Test start fails if model file is missing."""
    mocker.patch("link.server.load_json_config", side_effect=[device_config, runtime_config])
    mocker.patch("link.server.LogClient.get")
    mocker.patch("link.server.requests.put").return_value.status_code = 200

    if hasattr(ModelServer, "_is_port_in_use"):
        mocker.patch.object(ModelServer, "_is_port_in_use", return_value=False)
    else:
        mocker.patch("link.server.is_process_running", return_value=False)

    def _mock_exists(self):
        path_str = str(self)
        if path_str.endswith(".json"):
            return True
        if path_str.endswith(".gguf"):
            return False
        return True

    mocker.patch("pathlib.Path.exists", autospec=True, side_effect=_mock_exists)
    mocker.patch("pathlib.Path.mkdir")  # Fix: Mock mkdir so log setup doesn't fail

    mock_fail = mocker.patch("link.logger.fail")
    mock_popen = mocker.patch("subprocess.Popen")

    mocker.patch("builtins.open", mocker.mock_open())

    server = ModelServer(mock_payload)
    server.start()

    mock_fail.assert_called()
    assert "Model file not found" in mock_fail.call_args[0][0]
    mock_popen.assert_not_called()


def test_stop(mocker, mock_paths, device_config, mock_payload):
    """Test stop calls the utility function."""
    mocker.patch("link.server.load_json_config", return_value=device_config)
    mocker.patch("link.server.LogClient.get")
    mocker.patch("link.server.requests.put").return_value.status_code = 200

    mock_stop_tree = mocker.patch("link.server.stop_process_tree")

    mock_proc = mocker.MagicMock()

    server = ModelServer(mock_payload)
    server.process = mock_proc  # simulate running process

    server.stop()

    mock_proc.terminate.assert_called()
    if mock_stop_tree.called:
        mock_stop_tree.assert_called_with(server.pid_file, "Model Server")
