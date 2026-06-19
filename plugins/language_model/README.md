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
| `cors_allowed_origins` | serve | Non-empty list enables ACAO/CORS headers on the serving proxy. The proxy itself is always in front of llama-swap — it's also the inference-telemetry capture point — so this flag controls header behavior, not whether a proxy exists. |
| `system_prompt` | chat | Initial instruction. |

## Testing a served model — hit the proxy, not llama-swap

When the model is serving, two ports are listening:

| Port | What | Use this for |
|---|---|---|
| `8100` (configured host; default `127.0.0.1`) | ServingProxy → llama-swap | All real chat traffic. **Telemetry fires here.** |
| `8150` (loopback only) | llama-swap directly | Internal — proxy's upstream. **Bypasses telemetry.** |

llama-swap ships a built-in chat UI at `http://127.0.0.1:8150/ui/`. It's handy for triage but it talks to itself on `:8150`, so **chats from that UI never reach the ServingProxy** — no `model_inference_result_received` event, no row on the control UI's Inference Results page. If you're verifying observability, drive the model through `:8100`:

```bash
# The "model" field must be the model's display name (what llama-swap routes by),
# not the pipeline_id UUID. Get it from /v1/models:
curl http://localhost:8100/v1/models
# → {"data":[{"id":"<display_name>", ...}], ...}

curl -N -X POST http://localhost:8100/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<display_name from /v1/models>",
    "messages": [{"role":"user","content":"hello"}],
    "stream": true,
    "stream_options": {"include_usage": true},
    "max_tokens": 50
  }'
```

Backend attribution (Firestore, PostHog, control UI) still uses the pipeline_id UUID — the topic key the agent publishes on carries it, independently of what llama-swap routes by. SafeChat and any other production client already point at `:8100` by default — this caveat only affects manual testing via the bundled UI.
