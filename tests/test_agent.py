import pytest

import link.agent as agent


@pytest.fixture
def device_config():
    """Fixture providing common device configuration for tests."""
    return {
        "device_id": "test_device_123",
        "api_key": "test_api_key_abc",
        "base_url": "http://localhost:8000/api/v1",
    }


@pytest.fixture
def setup_agent(device_config):
    """Fixture to initialize agent configuration."""
    agent.BASE_URL = device_config["base_url"]


def test_activate_agent_success(setup_agent, device_config, mocker):
    """Test successful agent activation."""
    mock_get = mocker.patch("requests.get")
    mock_post = mocker.patch("requests.post")

    # Mock user verification response
    mock_get.return_value.status_code = 200

    # Mock activation response
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"api_key": device_config["api_key"]}

    # Mock file operations for saving config
    mock_file = mocker.patch("builtins.open", mocker.mock_open())
    mocker.patch("json.dump")

    result = agent.activate_agent(device_config["device_id"], "user_token")

    assert result is True
    mock_post.assert_called_with(
        f"{device_config['base_url']}/agent/activate",
        json={"device_id": device_config["device_id"]},
        headers={"Authorization": "Bearer user_token"},
    )
    # Verify config was saved
    assert mock_file.called


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
    mock_disk.return_value.free = 50 * (1024**3)  # 50GB

    metrics = agent.get_system_metrics()

    assert metrics["cpu_usage"] == 15.5
    assert metrics["ram_usage"] == 45.0
    assert metrics["storage_available_gb"] == 50.0
    assert metrics["temperature_celsius"] == 65.0


def test_send_metrics(device_config, mocker):
    """Test sending metrics to the backend."""
    mock_post = mocker.patch("requests.post")
    mock_post.return_value.status_code = 200

    mock_metrics = mocker.patch("link.agent.get_system_metrics")
    mock_metrics.return_value = {"cpu": 10}

    agent.send_metrics(device_config["device_id"], device_config["api_key"])

    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert f"/agent/{device_config['device_id']}/metrics" in args[0]
    assert kwargs["json"] == {"cpu": 10}


def test_execute_shell_command(device_config, mocker):
    """Test executing a shell command received from backend."""
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
    mock_run.assert_called_with("echo test", shell=True, capture_output=True, text=True, timeout=300)


def test_start_model_inference(device_config, mocker):
    """Test starting model inference process."""
    mock_popen = mocker.patch("subprocess.Popen")

    # Setup the process mock
    process_mock = mocker.MagicMock()
    process_mock.pid = 9999
    process_mock.poll.return_value = None

    process_mock.stdout.readline.return_value = ""
    process_mock.stderr.readline.return_value = ""

    mock_popen.return_value = process_mock

    # Mock file existence
    mocker.patch("pathlib.Path.exists", return_value=True)

    payload = {"model_name": "test_model.tflite", "model_id": "model_123"}
    config = {"device_id": device_config["device_id"]}
    running_procs = {}

    status, output = agent.start_model_inference(payload, device_config["api_key"], config, running_procs)

    assert status == "completed"
    assert "PID 9999" in output
    assert "test_model.tflite" in running_procs
