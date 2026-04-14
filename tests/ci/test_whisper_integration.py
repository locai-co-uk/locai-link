# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""CI test for the whisper-server lifecycle.

Downloads ggml-tiny and a short WAV sample, starts whisper-server directly,
verifies the health endpoint and a transcription request, then shuts down cleanly.

These tests are skipped during local runs (see pyproject.toml addopts)
and only execute in CI where the binary is installed.
"""

import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import psutil
import pytest
import requests

from link.serving.whisper_server import WhisperServer

# -- Constants ----------------------------------------------------------------
TEMP_DIR = Path(__file__).parent / "temp_whisper"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin"
MODEL_PATH = TEMP_DIR / "ggml-tiny.bin"
# JFK "Ask not what your country can do for you..." — 176 KB, reliably transcribed
AUDIO_URL = "https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/samples/jfk.wav"
AUDIO_PATH = TEMP_DIR / "jfk.wav"
TEST_PORT = 8098


# -- Fixtures -----------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def setup_teardown():
    """Download model and audio sample before tests, clean up after."""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    if not MODEL_PATH.exists():
        print(f"\n[CI] Downloading ggml-tiny to {MODEL_PATH}...")
        req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp, open(MODEL_PATH, "wb") as f:
            shutil.copyfileobj(resp, f)

    if not AUDIO_PATH.exists():
        print(f"[CI] Downloading JFK sample to {AUDIO_PATH}...")
        req = urllib.request.Request(AUDIO_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp, open(AUDIO_PATH, "wb") as f:
            shutil.copyfileobj(resp, f)

    yield

    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)


@pytest.fixture(scope="module")
def server_binary():
    """Locate the whisper-server binary using the same logic as WhisperServer."""
    binary = WhisperServer._get_server_binary(WhisperServer.__new__(WhisperServer))
    if binary is None:
        pytest.skip("whisper-server binary not installed — run manager.py setup first")
    return binary


# -- Tests --------------------------------------------------------------------
def test_whisper_server_lifecycle(server_binary):
    """Start whisper-server, check health, transcribe audio, then stop."""
    cmd = [
        str(server_binary),
        "--model",
        str(MODEL_PATH),
        "--host",
        "127.0.0.1",
        "--port",
        str(TEST_PORT),
    ]

    print(f"\n[CI] Starting whisper-server on port {TEST_PORT}...")
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )

    try:
        # Wait for server to become ready (returns 503 while loading, 200 when ready)
        health_url = f"http://127.0.0.1:{TEST_PORT}/health"
        deadline = time.time() + 120
        ready = False
        while time.time() < deadline:
            if process.poll() is not None:
                break  # process crashed — stop waiting
            try:
                resp = requests.get(health_url, timeout=2)
                if resp.status_code == 200:
                    ready = True
                    break
            except requests.ConnectionError:
                pass
            time.sleep(1)

        exit_code = process.poll()
        if not ready and exit_code is not None:
            assert False, f"Server crashed on startup (exit code {exit_code})"
        assert ready, "Server did not become healthy within 120 seconds"
        print("[CI] Health check passed")

        # Send a transcription request
        inference_url = f"http://127.0.0.1:{TEST_PORT}/inference"
        print("[CI] Sending transcription request...")
        with open(AUDIO_PATH, "rb") as audio_file:
            resp = requests.post(
                inference_url,
                files={"file": ("jfk.wav", audio_file, "audio/wav")},
                data={"response_format": "json"},
                timeout=60,
            )

        assert resp.status_code == 200, f"Inference returned {resp.status_code}: {resp.text}"

        text = resp.json().get("text", "").strip()
        print(f"[CI] Transcription: {text.encode('ascii', errors='replace').decode('ascii')}")
        assert len(text) > 0, "Transcription returned empty text"

    finally:
        # Clean shutdown — communicate() drains the pipe and waits for exit
        print("\n[CI] Stopping whisper-server...")
        pid = process.pid
        if process.poll() is None:
            process.terminate()
        try:
            out, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            out, _ = process.communicate()

        if out:
            tail = out[-3000:]
            print(f"[CI] Server output:\n{tail}")

        time.sleep(1)
        assert not psutil.pid_exists(pid), "Server process did not exit cleanly"
        print("[CI] Whisper server stopped and cleaned up")
