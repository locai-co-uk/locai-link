# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import atexit
import csv
import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd  # type: ignore

logger = logging.getLogger(__name__)

# 1. Backend Selection Chain
try:
    from ai_edge_litert.interpreter import Interpreter  # type: ignore
except ImportError:
    try:
        from tflite_runtime.interpreter import Interpreter  # type: ignore
    except ImportError:
        try:
            import tensorflow.lite as tflite  # type: ignore

            Interpreter = tflite.Interpreter
        except ImportError:
            Interpreter = None


# YAMNet Constants
SAMPLE_RATE = 16000
PATCH_WINDOW_SECONDS = 0.975
PATCH_HOP_SECONDS = 0.48


class AudioClassifier:
    def __init__(
        self,
        model_path,
        labels_path=None,
        device_index=None,
        confidence_threshold=0.3,
        min_duration=1.0,
        min_interval=0.5,
        **kwargs,
    ):
        self.model_path = self._resolve_path(model_path)
        self.labels_path = self._resolve_path(labels_path) if labels_path else None
        self.model_id = self.model_path.stem

        self.device_index = device_index
        self.threshold = float(confidence_threshold)
        self.min_duration = float(min_duration)
        self.min_interval = float(min_interval)

        self.labels = self._load_labels()
        self._load_backend()

        self.window_size = int(SAMPLE_RATE * PATCH_WINDOW_SECONDS)
        self.hop_size = int(SAMPLE_RATE * PATCH_HOP_SECONDS)
        self.buffer = np.zeros(self.window_size, dtype=np.float32)

        self.running = True
        self.stream = None

        self.audio_queue = queue.Queue(maxsize=10)

        atexit.register(self.stop)

        self.output_queue = queue.Queue(maxsize=1)
        self.current_event = None
        self.last_event_time = 0.0
        self.last_label = None

        self.inference_thread = threading.Thread(target=self._inference_loop, daemon=True)
        self.inference_thread.start()

        try:
            self._start_audio_stream()
            logger.info(f"{self.__class__.__name__} initialised via Callback Mode.")
        except Exception as e:
            self.stop()
            raise RuntimeError(f"Failed to initialise audio stream: {e}")

    def __call__(self):
        """Retrieves the next inference result (non-blocking)."""
        try:
            result = self.output_queue.get_nowait()
            return result
        except queue.Empty:
            # If the inference thread died, raise an error to the pipeline
            if not self.running or not self.inference_thread.is_alive():
                raise StopIteration("Audio inference thread stopped.")
            return None

    def stop(self):
        """Graceful Shutdown using Callback mechanism."""
        logger.info(f"Stopping {self.__class__.__name__}...")
        self.running = False

        try:
            atexit.unregister(self.stop)
        except Exception:
            pass

        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception as e:
                logger.warning(f"Error closing audio stream: {e}")
            finally:
                self.stream = None

        try:
            self.audio_queue.put(None)
        except Exception:
            pass

        if self.inference_thread.is_alive():
            self.inference_thread.join(timeout=1.0)
            if self.inference_thread.is_alive():
                logger.warning("Inference thread did not exit cleanly.")

        logger.info(f"{self.__class__.__name__} stopped.")

    # --- AUDIO CALLBACK (Runs in C-Thread) ---
    def _audio_callback(self, indata, frames, time, status):
        """
        Called by PortAudio whenever there is new audio data.
        This must return quickly! No inference here.
        """
        if status:
            logger.warning(f"Audio Status: {status}")

        if self.running:
            try:
                # We must copy() because indata is reused by PortAudio
                self.audio_queue.put(indata.copy(), block=False)
            except queue.Full:
                pass  # Drop frame if inference is too slow (better than crashing)

    def _start_audio_stream(self):
        """Initialises the non-blocking callback stream."""
        logger.info(f"Starting audio stream on device {self.device_index}...")
        self.stream = sd.InputStream(
            device=self.device_index,
            channels=1,
            samplerate=SAMPLE_RATE,
            blocksize=self.hop_size,  # Ensures we get exactly the chunk size we need
            dtype="float32",
            callback=self._audio_callback,
        )
        self.stream.start()

    # --- INFERENCE LOOP (Runs in Python Thread) ---
    def _inference_loop(self):
        """Consumes audio chunks from queue and runs inference."""
        while self.running:
            try:
                # Wait for data from the audio callback
                chunk = self.audio_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            # Check for Sentinel (Stop Signal)
            if chunk is None:
                break

            # Update Rolling Buffer
            self.buffer = np.roll(self.buffer, -self.hop_size)
            self.buffer[-self.hop_size :] = chunk.flatten()

            try:
                label, score = self._run_inference(self.buffer)

                payload = self._process_event_state(label, score)
                if payload:
                    if self.output_queue.full():
                        try:
                            self.output_queue.get_nowait()
                        except Exception:
                            pass
                    self.output_queue.put(payload)
                    logger.debug(f"Audio Detected: {payload['model_output']} ({payload['model_output_confidence']})")
            except Exception as e:
                logger.error(f"Inference error: {e}")

    # --- HELPER METHODS (Unchanged) ---
    def _resolve_path(self, path_str):
        path = Path(path_str)
        if path.exists():
            return path
        if (Path.cwd().parent / path_str).exists():
            return Path.cwd().parent / path_str
        if (Path.cwd() / path_str).exists():
            return Path.cwd() / path_str
        return path

    def _load_labels(self):
        if self.labels_path and self.labels_path.exists():
            target = self.labels_path
        else:
            target = self.model_path.parent / "yamnet_class_map.csv"

        if not target.exists():
            if requests:
                logger.info("Downloading YAMNet labels...")
                url = "https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv"
                try:
                    resp = requests.get(url, timeout=5)
                    resp.raise_for_status()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "w") as f:
                        f.write(resp.text)
                except Exception as e:
                    logger.warning(f"Could not download labels: {e}")
                    return [f"Class_{i}" for i in range(521)]
            else:
                return [f"Class_{i}" for i in range(521)]

        names = []
        try:
            with open(target, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    names.append(row["display_name"])
            return names
        except Exception as e:
            logger.error(f"Error parsing labels CSV: {e}")
            return [f"Class_{i}" for i in range(521)]

    def _load_backend(self):
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model not found: {self.model_path}")
        if not Interpreter:
            raise ImportError("TFLite runtime missing")

        logger.info(f"Loading TFLite model: {self.model_path.name}")
        self.interpreter = Interpreter(model_path=str(self.model_path))
        self.interpreter.allocate_tensors()
        self.input_details = self.interpreter.get_input_details()
        self.output_details = self.interpreter.get_output_details()
        self.input_idx = self.input_details[0]["index"]
        self.output_idx = self.output_details[0]["index"]

    def _run_inference(self, audio_waveform):
        input_data = audio_waveform.astype(np.float32)
        self.interpreter.set_tensor(self.input_idx, input_data)
        self.interpreter.invoke()
        output_data = self.interpreter.get_tensor(self.output_idx)[0]
        top_idx = np.argmax(output_data)
        score = float(output_data[top_idx])
        label = self.labels[top_idx] if top_idx < len(self.labels) else f"Class_{top_idx}"
        return label, score

    def _process_event_state(self, label, score):
        now = time.time()
        result = None
        if score >= self.threshold:
            if self.current_event and label == self.last_label:
                self.current_event["max_conf"] = max(self.current_event["max_conf"], score)
                self.current_event["end_ts"] = now
            else:
                if self.current_event:
                    result = self._finalise_event()
                self.current_event = {
                    "label": label,
                    "start_ts": now,
                    "end_ts": now,
                    "max_conf": score,
                }
                self.last_label = label
        else:
            if self.current_event:
                result = self._finalise_event()
                self.current_event = None
            self.last_label = None
        return result

    def _finalise_event(self):
        if not self.current_event:
            return None
        duration = self.current_event["end_ts"] - self.current_event["start_ts"]
        interval = time.time() - self.last_event_time
        if duration >= self.min_duration and interval >= self.min_interval:
            self.last_event_time = time.time()
            return {
                "model_id": self.model_id,
                "model_type": "classification",
                "sub_model_type": "audio_classification",
                "model_output_type": "text",
                "model_output": self.current_event["label"],
                "model_output_confidence": round(self.current_event["max_conf"], 4),
                "model_output_start_time": datetime.fromtimestamp(self.current_event["start_ts"]).isoformat(),
                "model_output_end_time": datetime.fromtimestamp(self.current_event["end_ts"]).isoformat(),
                "model_output_duration": round(duration, 2),
                "model_output_metadata": {"source": "live_audio"},
            }
        return None
