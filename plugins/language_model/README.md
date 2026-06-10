# Language Model Plugin

Local LLM inference via `llama-server` + `llama-swap` (from llama.cpp).

**Type:** `language_model`

## Args

| Key | Mode | Notes |
|---|---|---|
| `model_path` | both | Path to a `.gguf` file. |
| `mode` | both | `serve` (HTTP) or `chat` (interactive). |
| `port`, `host` | serve | Public HTTP listener. Default `8100`, `127.0.0.1`. |
| `n_gpu_layers`, `n_ctx` | both | Standard llama-cpp tunables. |
| `cors_allowed_origins` | serve | Non-empty list turns on a CORS proxy in front of llama-swap; empty/absent leaves llama-swap binding the public port directly. |
| `system_prompt` | chat | Initial instruction. |
