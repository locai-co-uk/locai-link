# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import link.agent as agent


def test_get_system_metrics(mocker):
    """Test system metrics collection."""
    mock_cpu = mocker.patch("psutil.cpu_percent")
    mock_mem = mocker.patch("psutil.virtual_memory")
    mock_disk = mocker.patch("psutil.disk_usage")
    mocker.patch(
        "psutil.sensors_temperatures",
        return_value={"cpu_thermal": [mocker.MagicMock(current=65.0)]},
        create=True,
    )

    mock_cpu.return_value = 15.5
    mock_mem.return_value.percent = 45.0
    mock_disk.return_value.free = 50 * (1024**3)

    metrics = agent.get_system_metrics()

    assert metrics["cpu_usage"] == 15.5
    assert metrics["ram_usage"] == 45.0
    assert metrics["storage_available_gb"] == 50.0
    assert metrics["temperature_celsius"] == 65.0


def test_send_metrics(mocker, mock_paths, device_config):
    """Test sending metrics to the backend."""
    mock_post = mocker.patch("requests.post")
    mock_post.return_value.status_code = 200

    mocker.patch("link.agent.get_system_metrics", return_value={"cpu": 10})

    agent.send_metrics(device_config["device_id"], device_config["api_key"])

    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert f"/agent/{device_config['device_id']}/metrics" in args[0]


def test_execute_shell_command(mocker, mock_paths, device_config):
    """Test executing a shell command."""
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "Command output"
    mock_run.return_value.stderr = ""

    command = {
        "id": "cmd_1",
        "data": {
            "command_type": "run_shell_command",
            "payload": {"command": "echo test"},
        },
    }

    status, output = agent.execute_command(command, device_config["api_key"], {"device_id": device_config["device_id"]})

    assert status == "completed"
    assert "Command output" in output
    mock_run.assert_called()


def test_deploy_model(mocker, mock_paths, device_config):
    """Test model deployment."""
    mocker.patch("pathlib.Path.mkdir")
    mock_get = mocker.patch("requests.get")
    mock_get.return_value.status_code = 200
    mock_get.return_value.iter_content.return_value = [b"data"]

    # Use mocker.mock_open
    m_open = mocker.mock_open()
    mocker.patch("builtins.open", m_open)

    mocker.patch("link.logger.LogClient.get")  # Mock logger

    payload = {"model_id": "m1", "model_name": "m.gguf", "file_extension": "gguf", "runtime_config": {}}

    agent.BASE_URL = device_config["api_url"]

    result = agent.deploy_model(payload, device_config["api_key"], device_config)
    assert result[0] == "completed"


def test_execute_start_serving(mocker, mock_paths, device_config):
    """Test start_serving routes to LLMServer for language_models (the default)."""
    mock_server_class = mocker.patch("link.agent.LLMServer")
    mock_instance = mock_server_class.return_value
    mock_instance.is_valid = True
    mock_instance.is_running.return_value = True

    command = {"id": "c1", "data": {"command_type": "start_serving", "payload": {"model_type": "language_models"}}}

    status, msg = agent.execute_command(command, "key", device_config)
    assert status == "completed"
    mock_instance.start.assert_called_once()


def test_execute_start_serving_whisper(mocker, mock_paths, device_config):
    """Test start_serving routes to WhisperServer for audio_transcription."""
    mock_server_class = mocker.patch("link.agent.WhisperServer")
    mock_instance = mock_server_class.return_value
    mock_instance.is_valid = True
    mock_instance.is_running.return_value = True

    command = {"id": "c3", "data": {"command_type": "start_serving", "payload": {"model_type": "audio_transcription"}}}

    status, msg = agent.execute_command(command, "key", device_config)
    assert status == "completed"
    mock_instance.start.assert_called_once()


def test_execute_stop_serving(mocker, mock_paths, device_config):
    """Test stop_serving routes to LLMServer for language_models (the default)."""
    mock_server_class = mocker.patch("link.agent.LLMServer")
    mock_instance = mock_server_class.return_value

    command = {"id": "c2", "data": {"command_type": "stop_serving", "payload": {"model_type": "language_models"}}}

    status, msg = agent.execute_command(command, "key", device_config)
    assert status == "completed"
    mock_instance.stop.assert_called_once()


def test_execute_stop_serving_whisper(mocker, mock_paths, device_config):
    """Test stop_serving routes to WhisperServer for audio_transcription."""
    mock_server_class = mocker.patch("link.agent.WhisperServer")
    mock_instance = mock_server_class.return_value

    command = {"id": "c4", "data": {"command_type": "stop_serving", "payload": {"model_type": "audio_transcription"}}}

    status, msg = agent.execute_command(command, "key", device_config)
    assert status == "completed"
    mock_instance.stop.assert_called_once()


def test_start_model_inference_success(mocker, mock_paths, device_config):
    """Test starting a background inference process."""
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("requests.post").return_value.status_code = 200

    # --- Subprocess Mock ---
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mocker.MagicMock()
    mock_proc.pid = 555

    # Handle infinite loop in threads by returning EOF
    mock_proc.stdout.readline.return_value = ""
    mock_proc.stderr.readline.return_value = ""

    mock_popen.return_value = mock_proc
    # -----------------------

    running_procs = {}
    payload = {"model_name": "test.tflite", "model_id": "m1"}

    status, msg = agent.start_model_inference(payload, "key", device_config, running_procs)

    assert status == "completed"
    assert "test.tflite" in running_procs
    assert running_procs["test.tflite"]["process"] == mock_proc
