# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""CI test for the llama-server lifecycle.

Downloads a small GGUF model, starts the server binary directly,
verifies the health endpoint and chat completion, then shuts down cleanly.

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

from link.server import ModelServer

# -- Constants ----------------------------------------------------------------
TEMP_DIR = Path(__file__).parent / "temp_models"
MODEL_URL = "https://huggingface.co/bartowski/SmolLM2-135M-Instruct-GGUF/resolve/main/SmolLM2-135M-Instruct-Q4_K_M.gguf"
MODEL_PATH = TEMP_DIR / "smollm2-135m.gguf"
TEST_PORT = 8099


# -- Fixtures -----------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def setup_teardown():
    """Download a tiny model before tests and clean up after."""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    if not MODEL_PATH.exists():
        print(f"\n[CI] Downloading SmolLM2 to {MODEL_PATH}...")
        req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp, open(MODEL_PATH, "wb") as f:
            shutil.copyfileobj(resp, f)
    yield
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)


@pytest.fixture(scope="module")
def server_binary():
    """Locate the llama-server binary using the same logic as ModelServer."""
    binary = ModelServer._get_server_binary(ModelServer.__new__(ModelServer))
    if binary is None:
        pytest.skip("llama-server binary not installed — run manager.py setup first")
    return binary


# -- Tests --------------------------------------------------------------------
def test_server_lifecycle(server_binary):
    """Start llama-server, check health, run a chat completion, then stop."""
    cmd = [
        str(server_binary),
        "--model",
        str(MODEL_PATH),
        "--host",
        "127.0.0.1",
        "--port",
        str(TEST_PORT),
        "--ctx-size",
        "512",
        "--n-gpu-layers",
        "0",
    ]

    print(f"\n[CI] Starting server on port {TEST_PORT}...")
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        # Wait for server to become ready
        health_url = f"http://127.0.0.1:{TEST_PORT}/health"
        deadline = time.time() + 60
        ready = False
        while time.time() < deadline:
            try:
                resp = requests.get(health_url, timeout=2)
                if resp.status_code == 200:
                    ready = True
                    break
            except requests.ConnectionError:
                pass
            time.sleep(1)

        assert ready, "Server did not become healthy within 60 seconds"
        print("[CI] Health check passed")

        # Send a chat completion
        chat_url = f"http://127.0.0.1:{TEST_PORT}/v1/chat/completions"
        payload = {
            "messages": [{"role": "user", "content": "Say hello."}],
            "max_tokens": 16,
        }
        print("[CI] Sending chat completion request...")
        resp = requests.post(chat_url, json=payload, timeout=30)
        assert resp.status_code == 200

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        print(f"[CI] Response: {content.encode('ascii', errors='replace').decode('ascii')}")
        assert len(content) > 0, "Model returned an empty response"

    finally:
        # Clean shutdown
        print("\n[CI] Stopping server...")
        pid = process.pid
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

        time.sleep(1)
        assert not psutil.pid_exists(pid), "Server process did not exit cleanly"
        print("[CI] Server stopped and cleaned up")
