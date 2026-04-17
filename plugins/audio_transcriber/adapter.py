# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import atexit
import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

# --- LOCAL IMPORTS ---
try:
    from .server import WhisperServer
except ImportError:
    from server import WhisperServer


logger = logging.getLogger(__name__)


class AudioTranscriber:
    """Plugin adapter for whisper.cpp audio transcription.

    Modes:
        - "serve": Starts whisper-server in background. External clients POST audio
          to /inference. The adapter monitors the server and yields heartbeat status.
        - "transcribe": Starts whisper-server, transcribes a local audio file, and
          yields the transcription result via the queue.
    """

    def __init__(
        self,
        model_path,
        mode="serve",
        host="0.0.0.0",
        port=8003,
        alias=None,
        audio_path=None,
        **kwargs,
    ):
        self.mode = mode
        self.queue = queue.Queue(maxsize=10)
        self.running = True
        self.model_path = self._resolve_path(model_path)
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model not found: {self.model_path}")

        self.model_id = alias or self.model_path.stem
        self.host = host
        self.port = int(port)
        self.audio_path = Path(audio_path) if audio_path else None

        self.server = WhisperServer(
            model_path=self.model_path,
            host=self.host,
            port=self.port,
            language=kwargs.get("language"),
            n_threads=kwargs.get("n_threads"),
            beam_size=kwargs.get("beam_size"),
        )
        self.server.start()

        atexit.register(self.stop)

        if self.mode == "serve":
            self.thread = threading.Thread(target=self._server_heartbeat_loop, daemon=True)
            self.thread.start()
        elif self.mode == "transcribe":
            if not self.audio_path or not self.audio_path.exists():
                self.stop()
                raise FileNotFoundError(f"Audio file not found: {self.audio_path}")
            self.thread = threading.Thread(target=self._transcribe_loop, daemon=True)
            self.thread.start()

    def __call__(self):
        """Retrieves the next result (non-blocking)."""
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            if not self.running or (hasattr(self, "thread") and not self.thread.is_alive()):
                raise StopIteration("Transcriber stopped.")
            return None

    def stop(self):
        self.running = False
        if self.server:
            self.server.stop()
        try:
            atexit.unregister(self.stop)
        except Exception:
            pass

    def transcribe(self, audio_path):
        """Sends an audio file to the running whisper-server and returns the transcription.

        Args:
            audio_path (str | Path): Path to the audio file.

        Returns:
            dict: Telemetry payload with transcription result, or None on failure.
        """
        audio_path = Path(audio_path)
        if not audio_path.exists():
            logger.error(f"Audio file not found: {audio_path}")
            return None

        url = f"http://{self.host}:{self.port}/inference"
        start_time = datetime.now()

        try:
            with open(audio_path, "rb") as f:
                resp = requests.post(
                    url,
                    files={"file": (audio_path.name, f, "audio/wav")},
                    data={"response_format": "json"},
                    timeout=120,
                )
            resp.raise_for_status()
            text = resp.json().get("text", "").strip()
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return None

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        return WhisperServer.build_telemetry_payload(
            model_id=self.model_id,
            output_text=text,
            start_time=start_time,
            end_time=end_time,
            duration=duration,
            metadata={"source": "file", "audio_file": audio_path.name},
        )

    def _server_heartbeat_loop(self):
        """Monitors whisper-server health in serve mode."""
        while self.running:
            if not self.server.running:
                logger.error("Whisper server process died!")
                self.running = False
                break
            time.sleep(10)

    def _transcribe_loop(self):
        """Transcribes the configured audio file and queues the result."""
        # Wait for server to be fully ready
        if not self.server.running:
            return

        result = self.transcribe(self.audio_path)
        if result and not self.queue.full():
            self.queue.put(result)

    def _resolve_path(self, path_str):
        path = Path(path_str)
        if path.exists():
            return path
        if (Path.cwd().parent / path_str).exists():
            return Path.cwd().parent / path_str
        return path
