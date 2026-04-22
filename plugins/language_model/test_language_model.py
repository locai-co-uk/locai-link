# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import shutil
import time
import urllib.request
from pathlib import Path

import psutil
import pytest
import requests
from link_language_model.adapter import LanguageModel  # type: ignore

# Constants
TEMP_DIR = Path(__file__).parent / "temp_models"
MODEL_URL = "https://huggingface.co/bartowski/SmolLM2-135M-Instruct-GGUF/resolve/main/SmolLM2-135M-Instruct-Q4_K_M.gguf"
MODEL_PATH = TEMP_DIR / "smollm2-135m.gguf"
TEST_PORT = 8099


@pytest.fixture(scope="module", autouse=True)
def setup_teardown():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    if not MODEL_PATH.exists():
        print(f"Downloading SmolLM2 to {MODEL_PATH}...")
        # User-Agent added to prevent 403s from some model hosts
        req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp, open(MODEL_PATH, "wb") as f:
            shutil.copyfileobj(resp, f)  # type: ignore
    yield
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)
    logs_dir = Path.cwd() / "logs"
    if logs_dir.exists():
        shutil.rmtree(logs_dir)


def test_llm_server_mode_lifecycle():
    """Starts the LLM in 'serve' mode, checks HTTP health, runs a completion, and shuts down."""
    print(f"\n[LLM] Starting Server on port {TEST_PORT}...")

    agent = LanguageModel(model_path=MODEL_PATH, mode="serve", port=TEST_PORT, n_gpu_layers=0, alias="test-model")

    # Server.start() is non-blocking; wait for the background health watcher to confirm readiness.
    assert agent.server.wait_until_ready(timeout=60), "Server did not become ready within 60s"

    try:
        assert agent.server.running
        pid = agent.server.process.pid
        assert psutil.pid_exists(pid), "Server process should exist"

        health_url = f"http://127.0.0.1:{TEST_PORT}/health"
        print(f"[LLM] Checking Health: {health_url}")
        resp = requests.get(health_url, timeout=5)
        assert resp.status_code == 200
        print("[LLM] Health Check Passed")

        chat_url = f"http://127.0.0.1:{TEST_PORT}/v1/chat/completions"
        payload = {"messages": [{"role": "user", "content": "Say 'hello'."}], "max_tokens": 10}
        print("[LLM] Sending Inference Request...")
        resp = requests.post(chat_url, json=payload, timeout=30)
        assert resp.status_code == 200
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        print(f"[LLM] Response: {content}")
        assert len(content) > 0

    finally:
        print("\n[LLM] Stopping server...")
        agent.stop()
        time.sleep(2)  # Give OS time to reap process

        assert not psutil.pid_exists(pid), "Zombie process detected! Server did not exit cleanly."
        assert not agent.server.monitor_thread.is_alive()
