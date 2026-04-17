# Language Model Plugin

Provides local LLM inference using `llama-cpp-python`.

## Component
**Type:** `language_model`

## Configuration
This component accepts standard GGUF parameters:
- `n_gpu_layers`: Layers to offload to GPU.
- `n_ctx`: Context window size.
- `system_prompt`: Initial instruction.