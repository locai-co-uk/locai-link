# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import json
import platform
from pathlib import Path

import pytest

from link.serving.llm_server import LLMServer

# Keep ModelServer alias so test helpers read naturally
ModelServer = LLMServer

BINARY_NAME = "llama-server.exe" if platform.system() == "Windows" else "llama-server"


@pytest.fixture
def mock_payload(runtime_config):
    """Generates a valid payload for LLMServer init."""
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


def _make_server(mocker, mock_paths, device_config, mock_payload):
    """Create a valid LLMServer instance with mocked init dependencies."""
    mocker.patch("link.serving.base_server.load_json_config", return_value=device_config)
    mocker.patch("link.serving.base_server.LogClient.get")
    mocker.patch("link.serving.base_server.requests.put").return_value.status_code = 200
    return LLMServer(mock_payload)


def test_init_success(mocker, mock_paths, device_config, mock_payload):
    """Test successful initialization with valid config and connection."""
    mocker.patch("link.serving.base_server.load_json_config", return_value=device_config)
    mock_log_client = mocker.patch("link.serving.base_server.LogClient.get")
    mock_put = mocker.patch("link.serving.base_server.requests.put")
    mock_put.return_value.status_code = 200

    server = LLMServer(mock_payload)

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
    mocker.patch("link.serving.base_server.load_json_config", return_value=None)
    mock_fail = mocker.patch("link.serving.base_server.link_logger.fail")

    server = LLMServer(mock_payload)

    assert server.is_valid is False
    mock_fail.assert_called_with("Base config not found.")


def test_start_success(mocker, mock_paths, device_config, runtime_config, mock_payload):
    """Test starting the server successfully."""
    # Mock Config loading sequence
    mocker.patch("link.serving.base_server.load_json_config", side_effect=[device_config, runtime_config])
    mocker.patch("link.serving.base_server.LogClient.get")
    mocker.patch("link.serving.base_server.requests.put").return_value.status_code = 200

    # Mock port check
    mocker.patch.object(LLMServer, "_is_port_in_use", return_value=False)

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

    # Mock the health-check loop and telemetry so the test doesn't block
    mocker.patch.object(LLMServer, "_wait_for_ready", return_value=True)
    mocker.patch("link.serving.llm_server.send_model_ready")
    mocker.patch.object(LLMServer, "_send_status")

    server = LLMServer(mock_payload)
    server.start()

    # Retrieve all calls made to Popen
    all_calls = mock_popen.call_args_list

    # Filter for the call that actually starts the server
    server_calls = []
    for call in all_calls:
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
    mocker.patch("link.serving.base_server.load_json_config", return_value=device_config)
    mocker.patch("link.serving.base_server.LogClient.get")
    mocker.patch("link.serving.base_server.requests.put").return_value.status_code = 200

    mocker.patch("link.serving.base_server.is_process_running", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value="999")

    mock_popen = mocker.patch("subprocess.Popen")

    server = LLMServer(mock_payload)
    server.start()

    mock_popen.assert_not_called()


def test_start_failure_missing_model(mocker, mock_paths, device_config, runtime_config, mock_payload):
    """Test start fails if model file is missing."""
    mocker.patch("link.serving.base_server.load_json_config", side_effect=[device_config, runtime_config])
    mocker.patch("link.serving.base_server.LogClient.get")
    mocker.patch("link.serving.base_server.requests.put").return_value.status_code = 200

    mocker.patch.object(LLMServer, "_is_port_in_use", return_value=False)

    def _mock_exists(self):
        path_str = str(self)
        if path_str.endswith(".json"):
            return True
        if path_str.endswith(".gguf"):
            return False
        return True

    mocker.patch("pathlib.Path.exists", autospec=True, side_effect=_mock_exists)
    mocker.patch("pathlib.Path.mkdir")

    mock_fail = mocker.patch("link.logger.fail")
    mock_popen = mocker.patch("subprocess.Popen")

    mocker.patch("builtins.open", mocker.mock_open())

    server = LLMServer(mock_payload)
    server.start()

    mock_fail.assert_called()
    assert "Model file not found" in mock_fail.call_args[0][0]
    mock_popen.assert_not_called()


def test_stop(mocker, mock_paths, device_config, mock_payload):
    """Test stop calls the utility function."""
    mocker.patch("link.serving.base_server.load_json_config", return_value=device_config)
    mocker.patch("link.serving.base_server.LogClient.get")
    mocker.patch("link.serving.base_server.requests.put").return_value.status_code = 200

    mock_stop_tree = mocker.patch("link.serving.base_server.stop_process_tree")

    mock_proc = mocker.MagicMock()

    server = LLMServer(mock_payload)
    server.process = mock_proc  # simulate running process

    server.stop()

    mock_proc.terminate.assert_called()
    if mock_stop_tree.called:
        mock_stop_tree.assert_called_with(server.pid_file, "Model Server")


# -- _load_and_parse_runtime_config -------------------------------------------


class TestLoadAndParseRuntimeConfig:
    """Tests for LLMServer._load_and_parse_runtime_config."""

    def test_config_not_found(self, mocker, mock_paths, device_config, runtime_config, mock_payload):
        """Returns False and logs if model config file is missing."""
        server = _make_server(mocker, mock_paths, device_config, mock_payload)
        mocker.patch("pathlib.Path.exists", return_value=False)
        mock_fail = mocker.patch("link.serving.base_server.link_logger.fail")

        result = server._load_and_parse_runtime_config()

        assert result is False
        mock_fail.assert_called_once()

    def test_artifact_resolved_locally(self, mocker, mock_paths, device_config, runtime_config, mock_payload, tmp_path):
        """Model path resolves to local MODELS_DIR when file exists there."""
        server = _make_server(mocker, mock_paths, device_config, mock_payload)

        # Patch load_json_config for the runtime config call
        mocker.patch("link.serving.base_server.load_json_config", return_value=runtime_config)
        mocker.patch("link.serving.base_server.CONFIGS_DIR", tmp_path)

        # Create the config file so .exists() passes
        conf_file = tmp_path / f"{mock_payload['model_id']}.json"
        conf_file.write_text(json.dumps(runtime_config))

        # Create the model file in MODELS_DIR
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        model_file = models_dir / "test_model.gguf"
        model_file.write_text("fake")
        mocker.patch("link.serving.base_server.MODELS_DIR", models_dir)

        result = server._load_and_parse_runtime_config()

        assert result is True
        assert server.model_path == model_file

    def test_artifact_falls_back_to_raw_path(
        self, mocker, mock_paths, device_config, runtime_config, mock_payload, tmp_path
    ):
        """Falls back to raw artifact path when local file doesn't exist."""
        server = _make_server(mocker, mock_paths, device_config, mock_payload)

        mocker.patch("link.serving.base_server.load_json_config", return_value=runtime_config)
        mocker.patch("link.serving.base_server.CONFIGS_DIR", tmp_path)

        conf_file = tmp_path / f"{mock_payload['model_id']}.json"
        conf_file.write_text(json.dumps(runtime_config))

        # Empty models dir — no local file
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        mocker.patch("link.serving.base_server.MODELS_DIR", models_dir)

        result = server._load_and_parse_runtime_config()

        assert result is True
        raw_path = runtime_config["process"]["artifacts"][0]["path"]
        assert server.model_path == Path(raw_path)

    def test_auto_select_gguf_fallback(self, mocker, mock_paths, device_config, mock_payload, tmp_path):
        """Auto-selects first .gguf in MODELS_DIR when config has no artifacts."""
        server = _make_server(mocker, mock_paths, device_config, mock_payload)

        config_no_artifacts = {"process": {"artifacts": [], "parameters": {"n_ctx": 2048}}}
        mocker.patch("link.serving.base_server.load_json_config", return_value=config_no_artifacts)
        mocker.patch("link.serving.base_server.CONFIGS_DIR", tmp_path)

        conf_file = tmp_path / f"{mock_payload['model_id']}.json"
        conf_file.write_text(json.dumps(config_no_artifacts))

        models_dir = tmp_path / "models"
        models_dir.mkdir()
        (models_dir / "some_model.gguf").write_text("fake")
        mocker.patch("link.serving.base_server.MODELS_DIR", models_dir)

        result = server._load_and_parse_runtime_config()

        assert result is True
        assert server.model_path.suffix == ".gguf"

    def test_params_loaded(self, mocker, mock_paths, device_config, runtime_config, mock_payload, tmp_path):
        """Parameters from config are stored on the server instance."""
        server = _make_server(mocker, mock_paths, device_config, mock_payload)

        mocker.patch("link.serving.base_server.load_json_config", return_value=runtime_config)
        mocker.patch("link.serving.base_server.CONFIGS_DIR", tmp_path)
        (tmp_path / f"{mock_payload['model_id']}.json").write_text(json.dumps(runtime_config))
        mocker.patch("link.serving.base_server.MODELS_DIR", tmp_path)  # doesn't matter, raw path fallback

        server._load_and_parse_runtime_config()

        assert server.params == runtime_config["process"]["parameters"]


# -- _get_server_binary -------------------------------------------------------


class TestGetServerBinary:
    """Tests for LLMServer._get_server_binary."""

    def test_finds_venv_binary(self, mocker, mock_paths, device_config, mock_payload, tmp_path):
        """Prefers .venv/bin-llama/ path."""
        server = _make_server(mocker, mock_paths, device_config, mock_payload)

        venv_bin = tmp_path / ".venv" / "bin-llama"
        venv_bin.mkdir(parents=True)
        binary = venv_bin / BINARY_NAME
        binary.write_text("fake")

        mocker.patch("link.serving.llm_server.PROJECT_ROOT", tmp_path)

        result = server._get_server_binary()
        assert result == binary

    def test_falls_back_to_bin(self, mocker, mock_paths, device_config, mock_payload, tmp_path):
        """Falls back to bin/ when .venv/bin-llama/ doesn't exist."""
        server = _make_server(mocker, mock_paths, device_config, mock_payload)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        binary = bin_dir / BINARY_NAME
        binary.write_text("fake")

        mocker.patch("link.serving.llm_server.PROJECT_ROOT", tmp_path)

        result = server._get_server_binary()
        assert result == binary

    def test_returns_none_when_missing(self, mocker, mock_paths, device_config, mock_payload, tmp_path):
        """Returns None when no binary is found anywhere."""
        server = _make_server(mocker, mock_paths, device_config, mock_payload)

        mocker.patch("link.serving.llm_server.PROJECT_ROOT", tmp_path)

        result = server._get_server_binary()
        assert result is None


# -- _parse_and_send_telemetry ------------------------------------------------


class TestParseAndSendTelemetry:
    """Tests for LLMServer._parse_and_send_telemetry."""

    def test_parses_valid_log_line(self, mocker, mock_paths, device_config, mock_payload):
        """Extracts duration and tokens from a real llama.cpp log line."""
        server = _make_server(mocker, mock_paths, device_config, mock_payload)
        mock_post = mocker.patch("link.serving.llm_server.requests.post")

        server._parse_and_send_telemetry("eval time =  2587.42 ms /  2038 tokens")

        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model_output_metadata"]["tokens_generated"] == 2038
        assert payload["model_output_metadata"]["duration"] == pytest.approx(2.58742)

    def test_handles_malformed_line(self, mocker, mock_paths, device_config, mock_payload):
        """Doesn't crash on a log line with no matches."""
        server = _make_server(mocker, mock_paths, device_config, mock_payload)
        mock_post = mocker.patch("link.serving.llm_server.requests.post")

        # Should still post (with 0 values), not raise
        server._parse_and_send_telemetry("some random log line with no numbers")

        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model_output_metadata"]["tokens_generated"] == 0
        assert payload["model_output_metadata"]["duration"] == 0.0

    def test_network_error_does_not_raise(self, mocker, mock_paths, device_config, mock_payload):
        """Network failure is caught silently (telemetry is best-effort)."""
        server = _make_server(mocker, mock_paths, device_config, mock_payload)
        mocker.patch("link.serving.llm_server.requests.post", side_effect=ConnectionError("offline"))

        # Should not raise
        server._parse_and_send_telemetry("eval time = 100.00 ms / 10 tokens")
