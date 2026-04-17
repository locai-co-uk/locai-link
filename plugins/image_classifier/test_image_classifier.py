# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import shutil
import time
import urllib.request
from pathlib import Path

import numpy as np
import pytest
from link_image_classifier.adapter import ImageClassifier

TEMP_DIR = Path(__file__).parent / "temp_models"
MODEL_URL = "https://raw.githubusercontent.com/tflite-soc/tensorflow-models/master/mobilenet-v1/mobilenet_v1_1.0_224_quant.tflite"
MODEL_PATH = TEMP_DIR / "mobilenet_v1.tflite"


@pytest.fixture(scope="module", autouse=True)
def setup_teardown():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    if not MODEL_PATH.exists():
        print(f"Downloading MobileNet to {MODEL_PATH}...")
        req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp, open(MODEL_PATH, "wb") as f:
            shutil.copyfileobj(resp, f)  # type: ignore
    yield
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)


def test_image_classifier_lifecycle(mocker):
    mock_cam = mocker.MagicMock()
    mock_cam.isOpened.return_value = True

    def slow_read():
        time.sleep(0.01)
        return (True, np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8))

    def close_camera():
        mock_cam.isOpened.return_value = False

    mock_cam.read.side_effect = slow_read
    mock_cam.release.side_effect = close_camera

    mocker.patch("cv2.VideoCapture", return_value=mock_cam)
    mocker.patch("cv2.getWindowProperty", return_value=1.0)
    mocker.patch("cv2.imshow")
    mocker.patch("cv2.waitKey", return_value=0)
    mocker.patch("cv2.destroyAllWindows")

    mocker.patch.object(ImageClassifier, "_run_inference", return_value=("cat", 0.99))

    classifier = ImageClassifier(model_path=MODEL_PATH, camera_index=0, confidence_threshold=0.01, show_window=False)

    assert classifier.interpreter is not None
    assert classifier.input_details is not None

    time.sleep(0.5)

    result = classifier()
    if result:
        assert result["model_output"] == "cat"

    print("\n[Vision] Stopping classifier...")
    start_time = time.time()
    classifier.stop()

    assert time.time() - start_time < 2.0, "Stop hung too long"
    assert not classifier.thread.is_alive(), "Thread did not exit cleanly"
    mock_cam.release.assert_called()
