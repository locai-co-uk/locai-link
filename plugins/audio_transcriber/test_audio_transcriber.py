# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Integration test for the audio_transcriber plugin.

Downloads ggml-tiny and a short WAV sample, starts whisper-server,
verifies health + transcription, then shuts down cleanly.
"""

import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
import pytest
import requests
from link_audio_transcriber.adapter import AudioTranscriber  # type: ignore

# Constants
TEMP_DIR = Path(__file__).parent / "temp_models"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin"
MODEL_PATH = TEMP_DIR / "ggml-tiny.bin"
AUDIO_URL = "https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/samples/jfk.wav"
AUDIO_PATH = TEMP_DIR / "jfk.wav"
TEST_PORT = 8098


def _download(url: str, path: Path, label: str, max_attempts: int = 5) -> bool:
    """Download url to path, retrying on 429 with jittered backoff.

    Returns False when 429 retries are exhausted (CDN rate-limiting — caller
    should skip). Non-429 HTTP errors raise so genuine bugs surface.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req) as resp, open(path, "wb") as f:
                shutil.copyfileobj(resp, f)  # type: ignore
            return True
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            if attempt == max_attempts:
                print(f"\n{label} download failed after {max_attempts} attempts (HTTP 429).")
                return False
            retry_after = int(exc.headers.get("Retry-After", 0))
            wait = max(retry_after, min(2**attempt, 60))
            print(f"\nRate-limited downloading {label} (attempt {attempt}/{max_attempts}); retrying in {wait}s...")
            time.sleep(wait)
    return False


@pytest.fixture(scope="module", autouse=True)
def setup_teardown():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    for url, path, label in [
        (MODEL_URL, MODEL_PATH, "ggml-tiny"),
        (AUDIO_URL, AUDIO_PATH, "JFK sample"),
    ]:
        if not path.exists():
            print(f"\nDownloading {label} to {path}...")
            if not _download(url, path, label):
                # Skip, don't fail — CDN rate-limiting (429) shouldn't block
                # unrelated PR merges.
                pytest.skip(f"{label} CDN unavailable; skipping audio_transcriber integration test.")

    yield

    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)
    logs_dir = Path.cwd() / "logs"
    if logs_dir.exists():
        shutil.rmtree(logs_dir)


def test_whisper_serve_mode_lifecycle():
    """Starts whisper in serve mode, checks health, transcribes audio, shuts down."""
    print(f"\n[Whisper] Starting Server on port {TEST_PORT}...")

    agent = AudioTranscriber(model_path=MODEL_PATH, mode="serve", port=TEST_PORT)

    # Server.start() is non-blocking; wait for the background health watcher to confirm readiness.
    assert agent.server.wait_until_ready(timeout=60), "Server did not become ready within 60s"

    pid = None
    try:
        assert agent.server.running, "Server failed to start — check whisper-server logs"
        pid = agent.server.process.pid
        assert psutil.pid_exists(pid), "Server process should exist"

        # Health check
        health_url = f"http://127.0.0.1:{TEST_PORT}/health"
        print(f"[Whisper] Checking Health: {health_url}")
        resp = requests.get(health_url, timeout=5)
        assert resp.status_code == 200
        print("[Whisper] Health Check Passed")

        # Transcription via adapter method
        print("[Whisper] Sending Transcription Request...")
        result = agent.transcribe(AUDIO_PATH)
        assert result is not None
        text = result["model_output"]
        print(f"[Whisper] Transcription: {text}")
        assert len(text) > 0

        # Verify telemetry structure
        assert result["model_type"] == "generation"
        assert result["sub_model_type"] == "audio_transcription"
        assert "model_output_duration" in result
        assert result["model_output_metadata"]["source"] == "file"

    finally:
        print("\n[Whisper] Stopping server...")
        agent.stop()
        time.sleep(2)

        if pid is not None:
            assert not psutil.pid_exists(pid), "Zombie process detected! Server did not exit cleanly."
            assert not agent.server.monitor_thread.is_alive()


def test_whisper_transcribe_mode():
    """Starts whisper in transcribe mode with a specific audio file."""
    print(f"\n[Whisper] Starting Transcribe mode on port {TEST_PORT}...")

    agent = AudioTranscriber(
        model_path=MODEL_PATH,
        mode="transcribe",
        audio_path=AUDIO_PATH,
        port=TEST_PORT,
    )

    try:
        assert agent.server.running, "Server failed to start — check whisper-server logs"

        # Wait for the transcription to complete
        result = None
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                result = agent()
            except StopIteration:
                pytest.fail("Transcriber thread exited before producing a result")
            if result is not None:
                break
            time.sleep(1)

        assert result is not None, "Transcription did not produce a result within timeout"
        text = result["model_output"]
        print(f"[Whisper] Transcription: {text}")
        assert len(text) > 0

    finally:
        agent.stop()
