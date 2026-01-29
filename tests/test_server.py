# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

from link.server import ModelServer


def test_init_success(mocker, mock_paths, device_config):
    """Test successful initialization with valid config and connection."""
    mocker.patch("link.server.load_json_config", return_value=device_config)
    mock_log_client = mocker.patch("link.server.LogClient.get")
    mock_put = mocker.patch("requests.put")
    mock_put.return_value.status_code = 200

    server = ModelServer()

    assert server.is_valid is True
    assert server.device_id == device_config["device_id"]

    mock_log_client.return_value.configure.assert_called_with(
        device_config["device_id"], device_config["api_key"], device_config["api_url"]
    )


def test_init_failure_no_config(mocker, mock_paths):
    """Test initialization failing due to missing agent_config.json."""
    mocker.patch("link.server.load_json_config", return_value=None)
    mock_fail = mocker.patch("link.logger.fail")

    server = ModelServer()

    assert server.is_valid is False
    mock_fail.assert_called_with("Base config not found.", category="process", action="init_server", hint=mocker.ANY)


def test_init_failure_unauthorized(mocker, mock_paths, device_config):
    """Test initialization failing due to 401 from API."""
    mocker.patch("link.server.load_json_config", return_value=device_config)
    mocker.patch("link.server.LogClient.get")

    mock_put = mocker.patch("requests.put")
    mock_put.return_value.status_code = 401

    mock_fail = mocker.patch("link.logger.fail")

    server = ModelServer()

    assert server.is_valid is False
    mock_fail.assert_called()
    assert "Unauthorized" in mock_fail.call_args[0][0]


def test_start_success(mocker, mock_paths, device_config, runtime_config):
    """Test starting the server successfully."""
    # Mock Config loading sequence
    mocker.patch("link.server.load_json_config", side_effect=[device_config, runtime_config])
    mocker.patch("link.server.LogClient.get")
    mocker.patch("requests.put").return_value.status_code = 200
    mocker.patch("link.server.is_process_running", return_value=False)

    # Path.exists mock
    mocker.patch("pathlib.Path.exists", return_value=True)

    # Mock subprocess using mocker.MagicMock
    mock_popen = mocker.patch("subprocess.Popen")
    mock_process = mocker.MagicMock()
    mock_process.pid = 12345
    mock_process.poll.return_value = None
    mock_popen.return_value = mock_process

    # Mock file operations using mocker.mock_open
    mocker.patch("pathlib.Path.write_text")
    mocker.patch("builtins.open", mocker.mock_open())

    server = ModelServer()
    server.start()

    mock_popen.assert_called_once()
    args = mock_popen.call_args[0][0]

    # Verify values from runtime_config
    assert "--port" in args
    assert str(runtime_config["serving"]["default_port"]) in args
    assert "--n_gpu_layers" in args
    assert str(runtime_config["process"]["parameters"]["n_gpu_layers"]) in args


def test_start_already_running(mocker, mock_paths, device_config):
    """Test start is skipped if process is already running."""
    mocker.patch("link.server.load_json_config", return_value=device_config)
    mocker.patch("link.server.LogClient.get")
    mocker.patch("requests.put").return_value.status_code = 200

    mocker.patch("link.server.is_process_running", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value="999")

    mock_popen = mocker.patch("subprocess.Popen")

    server = ModelServer()
    server.start()

    mock_popen.assert_not_called()


def test_start_failure_missing_model(mocker, mock_paths, device_config, runtime_config):
    """Test start fails if model file is missing."""
    mocker.patch("link.server.load_json_config", side_effect=[device_config, runtime_config])
    mocker.patch("link.server.LogClient.get")
    mocker.patch("requests.put").return_value.status_code = 200
    mocker.patch("link.server.is_process_running", return_value=False)

    # Robust Path.exists mock
    def _mock_exists(self):
        return "model" not in str(self)  # Configs exist, model doesn't

    mocker.patch("pathlib.Path.exists", autospec=True, side_effect=_mock_exists)

    mock_fail = mocker.patch("link.logger.fail")
    mock_popen = mocker.patch("subprocess.Popen")

    server = ModelServer()
    server.start()

    mock_fail.assert_called()
    assert "Model file not found" in mock_fail.call_args[0][0]
    mock_popen.assert_not_called()


def test_stop(mocker, mock_paths, device_config):
    """Test stop calls the utility function."""
    mocker.patch("link.server.load_json_config", return_value=device_config)
    mocker.patch("link.server.LogClient.get")
    mocker.patch("requests.put").return_value.status_code = 200

    mock_stop_tree = mocker.patch("link.server.stop_process_tree")

    server = ModelServer()
    server.stop()

    mock_stop_tree.assert_called_once()
