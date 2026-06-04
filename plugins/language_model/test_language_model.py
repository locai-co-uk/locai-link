# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
import pytest
import requests
from link_language_model.adapter import LanguageModel  # type: ignore

# The LLM under test can return Unicode characters in completions (emojis are
# common in SmolLM2 outputs). On Windows runners stdout defaults to cp1252,
# which can't encode those characters; subsequent print() calls then crash
# with UnicodeEncodeError even though the test logic itself passed. Force
# UTF-8 with backslash-escape fallback so debug prints don't fail the test.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

# Constants
TEMP_DIR = Path(__file__).parent / "temp_models"
MODEL_URL = "https://huggingface.co/bartowski/SmolLM2-135M-Instruct-GGUF/resolve/main/SmolLM2-135M-Instruct-Q4_K_M.gguf"
MODEL_PATH = TEMP_DIR / "smollm2-135m.gguf"
TEST_PORT = 8099


def _download_smollm2_with_retry(max_attempts: int = 4) -> bool:
    """Fetch the SmolLM2 GGUF model; tolerate transient CDN flakes.

    huggingface.co rate-limits anonymous downloads (returns HTTP 429) when
    parallel CI runs hit it in close succession. Retry with exponential
    backoff, then return False so the caller can skip the test rather
    than fail it.
    """
    req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, open(MODEL_PATH, "wb") as f:
                shutil.copyfileobj(resp, f)
            return True
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if attempt == max_attempts - 1:
                print(f"SmolLM2 download failed after {max_attempts} attempts: {exc}")
                return False
            backoff = 2 ** attempt  # 1s, 2s, 4s, 8s
            print(f"SmolLM2 download attempt {attempt + 1} failed ({exc}); retrying in {backoff}s")
            time.sleep(backoff)
    return False


@pytest.fixture(scope="module", autouse=True)
def setup_teardown():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    if not MODEL_PATH.exists():
        print(f"Downloading SmolLM2 to {MODEL_PATH}...")
        if not _download_smollm2_with_retry():
            # Skip rather than fail — the model lives on an external CDN
            # we don't control, and a transient 429/outage shouldn't block
            # unrelated PR merges. Mirrors the behaviour of audio_classifier
            # test_audio_classifier.py.
            pytest.skip("SmolLM2 CDN unavailable; skipping language_model integration test.")
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
    swap_mode = agent._swap_manager is not None

    # Wait for the serving endpoint to become ready (works for both swap and direct mode).
    # llama-swap loads the model lazily on first request, so the health check here only
    # confirms the proxy process is up; the inference step below forces model load.
    assert agent.wait_until_ready(timeout=60), "Serving endpoint did not become ready within 60s"

    pid = None
    try:
        if not swap_mode:
            assert agent.server is not None and agent.server.running
            pid = agent.server.process.pid
            assert psutil.pid_exists(pid), "Server process should exist"

        health_url = f"http://127.0.0.1:{TEST_PORT}/health"
        print(f"[LLM] Checking Health: {health_url}")
        resp = requests.get(health_url, timeout=5)
        assert resp.status_code == 200
        print("[LLM] Health Check Passed")

        chat_url = f"http://127.0.0.1:{TEST_PORT}/v1/chat/completions"
        # model field required by llama-swap for routing; ignored by direct llama-server
        payload = {"model": "test-model", "messages": [{"role": "user", "content": "Say 'hello'."}], "max_tokens": 10}
        print("[LLM] Sending Inference Request...")
        # Longer timeout for swap mode: first request triggers llama-server start + model load
        resp = requests.post(chat_url, json=payload, timeout=120)
        assert resp.status_code == 200
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        print(f"[LLM] Response: {content}")
        assert len(content) > 0

    finally:
        print("\n[LLM] Stopping server...")
        agent.stop()
        time.sleep(2)  # Give OS time to reap process

        if not swap_mode and pid is not None:
            assert not psutil.pid_exists(pid), "Zombie process detected! Server did not exit cleanly."
            assert agent.server is not None and not agent.server.monitor_thread.is_alive()
