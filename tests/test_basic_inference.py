# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

from pathlib import Path

import pytest

import link.inference.dispatcher as dispatcher


def test_determine_script_audio():
    """Test heuristic detection for audio models."""
    script = dispatcher.determine_inference_script_by_name("yamnet_audio_classifier.tflite")
    assert "audio" in script.name

    script = dispatcher.determine_inference_script_by_name("my_voice_model.tflite")
    assert "audio" in script.name


def test_determine_script_image():
    """Test heuristic detection for image models (default)."""
    script = dispatcher.determine_inference_script_by_name("mobilenet_v2.tflite")
    assert "image" in script.name

    script = dispatcher.determine_inference_script_by_name("object_detection.tflite")
    assert "image" in script.name


def test_determine_script_by_config(mocker):
    """Test selection based on config file with mocked filesystem."""
    mocker.patch.object(Path, "exists", return_value=True)

    config = {"process": {"impl": {"runner": "tflite_audio_classification"}}}
    script = dispatcher.determine_inference_script_by_config(config)
    assert "audio" in script.name

    config_invalid = {"process": {"impl": {"runner": "invalid_runner"}}}
    with pytest.raises(FileNotFoundError) as excinfo:
        dispatcher.determine_inference_script_by_config(config_invalid)

    assert "invalid_runner" in str(excinfo.value)


def test_run_inference_script(mocker):
    """Test subprocess launching by mocking file system existence."""
    mock_popen = mocker.patch("subprocess.Popen")
    mock_popen.return_value = mocker.MagicMock(pid=123)

    mock_exists = mocker.patch.object(Path, "exists")
    mock_exists.return_value = True

    script_path = Path("fake_script.py")
    model_path = Path("fake_model.tflite")

    _process = dispatcher.run_inference_script(script_path, model_path, "dev1", "key1")

    mock_popen.assert_called_once()
    args = mock_popen.call_args[0][0]

    assert str(script_path) in args
    assert str(model_path) in args
    assert "--device-id" in args
    assert "dev1" in args
    assert "--api-key" in args
    assert "key1" in args
