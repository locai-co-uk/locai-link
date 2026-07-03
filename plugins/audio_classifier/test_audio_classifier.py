# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import logging
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

# Absolute import works best when running 'pytest' from project root
from link_audio_classifier.adapter import AudioClassifier  # type: ignore

TEMP_DIR = Path(__file__).parent / "temp_models"
MODEL_URL = "https://huggingface.co/thelou1s/yamnet/resolve/main/lite-model_yamnet_classification_tflite_1.tflite"
MODEL_PATH = TEMP_DIR / "yamnet.tflite"


def _download_yamnet_with_retry(max_attempts: int = 4) -> bool:
    """Fetch the YAMNet TFLite model; tolerate transient CDN flakes.

    huggingface.co rate-limits anonymous downloads (returns HTTP 429) when
    parallel CI runs hit it in close succession. Retry with exponential
    backoff, then bail with a skip-the-test signal so a transient outage
    doesn't fail an unrelated PR.
    """
    req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, open(MODEL_PATH, "wb") as f:
                shutil.copyfileobj(resp, f)
            return True
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if attempt == max_attempts - 1:
                print(f"YAMNet download failed after {max_attempts} attempts: {exc}")
                return False
            backoff = 2**attempt  # 1s, 2s, 4s, 8s
            print(f"YAMNet download attempt {attempt + 1} failed ({exc}); retrying in {backoff}s")
            time.sleep(backoff)
    return False


@pytest.fixture(scope="module", autouse=True)
def setup_teardown():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    if not MODEL_PATH.exists():
        print(f"Downloading YAMNet to {MODEL_PATH}...")
        if not _download_yamnet_with_retry():
            # Skip rather than fail — this is a network-dependent integration
            # test whose model lives on an external CDN we don't control.
            # A transient 429 / outage shouldn't block unrelated PR merges.
            pytest.skip("YAMNet CDN unavailable; skipping audio_classifier integration test.")
    yield
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)


def test_audio_callback_architecture(mocker, caplog):
    """
    Verifies the new Callback-based architecture.
    """
    mock_stream_cls = mocker.patch("sounddevice.InputStream")
    _mock_stream_instance = mock_stream_cls.return_value

    classifier = AudioClassifier(model_path=MODEL_PATH, confidence_threshold=0.01, min_duration=0.0, min_interval=0.0)

    _args, kwargs = mock_stream_cls.call_args
    audio_callback = kwargs.get("callback")
    assert audio_callback is not None, "Adapter did not register a callback!"

    assert classifier.inference_thread.is_alive()
    assert classifier.running

    fake_audio = np.random.uniform(-1.0, 1.0, (classifier.hop_size, 1)).astype("float32")

    audio_callback(fake_audio, classifier.hop_size, None, None)

    time.sleep(1.0)

    result = classifier()
    if result:
        print(f"[Audio] Detected: {result['model_output']}")
    else:
        print("[Audio] Pipeline processed data (no specific class detected)")

    print("\n[Audio] Stopping classifier...")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        classifier.stop()

    assert not classifier.inference_thread.is_alive()

    captured_errors = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("stuck" in msg for msg in captured_errors)
