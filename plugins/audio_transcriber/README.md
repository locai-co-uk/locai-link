# Audio Transcriber Plugin

Local audio transcription using [whisper.cpp](https://github.com/ggml-org/whisper.cpp) for the Loc.ai platform.

## Installation

```bash
uv run install.py
```

This downloads (or builds from source) the `whisper-server` binary, pinned to a vetted release: the `WHISPER_CPP_RELEASE` constant in `install.py` is the single source of truth for the tag.

## Usage

The plugin exposes an `AudioTranscriber` class that manages the whisper-server lifecycle and provides a queue-based interface for transcription results.

### Serve Mode

Starts whisper-server as a background process. Clients can POST audio files to the `/inference` endpoint.

```python
from link_audio_transcriber.adapter import AudioTranscriber

transcriber = AudioTranscriber(
    model_path="models/ggml-base.bin",
    mode="serve",
    port=8003,
)
```

### Transcribe Mode

Starts whisper-server and transcribes a local audio file, returning the result via the queue.

```python
transcriber = AudioTranscriber(
    model_path="models/ggml-base.bin",
    mode="transcribe",
    audio_path="samples/recording.wav",
    port=8003,
)

result = transcriber()  # Returns telemetry dict or None
```

## API

Once the server is running, the whisper.cpp HTTP API is available:

- `GET /health` — Returns 200 when the server is ready.
- `POST /inference` — Accepts an audio file upload, returns transcribed text as JSON.
