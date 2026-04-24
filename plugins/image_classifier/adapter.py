# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import atexit
import logging
import os
import platform
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

if platform.system() == "Darwin":
    os.environ.setdefault("OPENCV_AVFOUNDATION_SKIP_AUTH", "1")

import cv2  # type: ignore  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402

logger = logging.getLogger(__name__)


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


class ImageClassifier:
    def __init__(
        self,
        model_path,
        labels_path=None,
        camera_index=0,
        confidence_threshold=0.6,
        min_duration=1.0,
        min_interval=0.5,
        show_window=False,
        width=None,
        height=None,
        camera_warmup_timeout=10.0,
        **kwargs,
    ):
        # 1. Configuration & Paths
        self.model_path = self._resolve_path(model_path)
        self.labels_path = self._resolve_path(labels_path) if labels_path else None
        self.model_id = self.model_path.stem

        self.camera_index = int(camera_index)
        self.threshold = float(confidence_threshold)
        self.min_duration = float(min_duration)
        self.min_interval = float(min_interval)
        self.show_window = show_window
        self.cam_width = width
        self.cam_height = height
        # macOS cold-opens on a shared camera regularly exceed the old 2s default.
        self.camera_warmup_timeout = float(camera_warmup_timeout)

        # 2. Load Static Resources
        self.labels = self._load_labels()
        self._load_backend()

        # 3. State Management
        self.running = True
        self.cap = None

        # Ensure stop is called on exit to release camera
        atexit.register(self.stop)

        self.queue = queue.Queue(maxsize=1)
        self.current_event = None
        self.last_event_time = 0.0
        self.last_label = None

        # Synchronisation Events
        self._camera_ready = threading.Event()
        self._startup_error = None

        # Daemon=True allows Ctrl-C to kill the process if it hangs
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()

        logger.info(f"Waiting for camera warmup (up to {self.camera_warmup_timeout:.0f}s)...")
        if not self._camera_ready.wait(timeout=self.camera_warmup_timeout):
            self.stop()
            error = self._startup_error or "Camera startup timed out (Resource busy?)"
            raise RuntimeError(f"Failed to start camera: {error}")

        logger.info(f"{self.__class__.__name__} initialised. Camera {self.camera_index} is active.")

    def __call__(self):
        """Retrieves the next inference result (blocking/non-blocking via queue)."""
        # 1. Try to get data
        try:
            result = self.queue.get_nowait()
            return result
        except queue.Empty:
            # 2. If no data, CHECK IF WE ARE DEAD
            # This is the fix: If the thread stopped (window closed), tell the Runtime to stop.
            if not self.running or not self.thread.is_alive():
                raise StopIteration("User closed the window.")
            return None

    def stop(self):
        """
        Graceful Shutdown with Force Quit fallback.
        """
        logger.info(f"Stopping {self.__class__.__name__}...")
        self.running = False

        try:
            atexit.unregister(self.stop)
        except Exception:
            pass

        # Force release camera to break the cap.read() block
        if self.cap and self.cap.isOpened():
            logger.debug("Forcing camera release to unblock thread...")
            self.cap.release()

        # Wait for thread to finish (max 1s)
        if self.thread.is_alive():
            self.thread.join(timeout=1.0)
            if self.thread.is_alive():
                logger.warning("Thread stuck; exiting anyway (daemon mode).")

        logger.info(f"{self.__class__.__name__} stopped.")

    def _resolve_path(self, path_str):
        """Resolves file paths relative to CWD or parent.

        Args:
            path_str: The input path string.

        Returns:
            Path: The resolved absolute path.
        """
        path = Path(path_str)
        if path.exists():
            return path
        if (Path.cwd().parent / path_str).exists():
            return Path.cwd().parent / path_str
        if (Path.cwd() / path_str).exists():
            return Path.cwd() / path_str
        return path

    def _load_labels(self):
        """Loads or downloads ImageNet labels.

        Returns:
            list[str]: The label names.
        """
        if self.labels_path and self.labels_path.exists():
            with open(self.labels_path, "r") as f:
                return [line.strip() for line in f.readlines()]

        target = self.labels_path or (self.model_path.parent / "imagenet_labels.txt")
        if target.exists():
            logger.info(f"Loading cached labels from {target}")
            with open(target, "r") as f:
                return [line.strip() for line in f.readlines()]

        if requests:
            logger.info(f"Downloading labels to {target}...")
            try:
                url = "https://storage.googleapis.com/download.tensorflow.org/data/ImageNetLabels.txt"
                resp = requests.get(url, timeout=5)
                resp.raise_for_status()
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "w") as f:
                    f.write(resp.text)
                return [line.strip() for line in resp.text.splitlines()]
            except Exception as e:
                logger.warning(f"Could not download labels: {e}. Using generic fallback.")
        else:
            logger.warning("'requests' library missing. Cannot download labels.")

        return [f"Class {i}" for i in range(1001)]

    def _load_backend(self):
        """Initialises the TFLite interpreter."""
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model not found: {self.model_path}")

        ext = self.model_path.suffix.lower()

        if ext == ".tflite":
            if not Interpreter:
                raise ImportError("TFLite runtime missing")
            self.backend_type = "tflite"
            self.interpreter = Interpreter(model_path=str(self.model_path))
            self.interpreter.allocate_tensors()
            self.input_details = self.interpreter.get_input_details()
            self.output_details = self.interpreter.get_output_details()
            self.quant_params = self.output_details[0].get("quantization", (0.0, 0))
            self.input_shape = self.input_details[0]["shape"]
            self.input_h = self.input_shape[1]
            self.input_w = self.input_shape[2]
            self.input_idx = self.input_details[0]["index"]
            self.output_idx = self.output_details[0]["index"]
            self.is_quantized = self.input_details[0]["dtype"] == np.uint8
        else:
            raise ValueError(f"Unsupported model: {ext}")

    def _capture_loop(self):
        """
        The main worker loop. Encapsulated in try/finally to ensure cleanup.
        """
        try:
            self.cap = cv2.VideoCapture(self.camera_index)
            if self.cam_width:
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cam_width)
            if self.cam_height:
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cam_height)

            if not self.cap.isOpened():
                self._startup_error = f"Cannot open index {self.camera_index}"
                self._camera_ready.set()
                return

            self._camera_ready.set()

            while self.running:
                if not self.cap.isOpened():
                    break

                ret, frame = self.cap.read()
                if not ret:
                    break

                try:
                    label, score = self._run_inference(frame)

                    payload = self._process_event_state(label, score)
                    if payload:
                        if self.queue.full():
                            try:
                                self.queue.get_nowait()
                            except Exception:
                                pass
                        self.queue.put(payload)
                        logger.debug(f"Detected: {payload['model_output']} ({payload['model_output_confidence']})")

                    if self.show_window:
                        self._draw_debug(frame, label, score)

                        # 1. Check for 'q' key
                        if cv2.waitKey(1) & 0xFF == ord("q"):
                            logger.info("Exiting via 'q' key...")
                            self.running = False

                        # 2. Check for Window 'X' close button
                        # WND_PROP_VISIBLE returns < 1 if closed
                        try:
                            if cv2.getWindowProperty("Loc.ai Vision", cv2.WND_PROP_VISIBLE) < 1:
                                logger.info("Window closed by user. Exiting...")
                                self.running = False
                        except Exception:
                            # Handle rare race condition if window is already destroyed
                            self.running = False

                except Exception as e:
                    logger.error(f"Inference error: {e}")
                    time.sleep(0.1)

        except Exception as e:
            logger.error(f"Crash in capture thread: {e}")
            self._startup_error = str(e)
            self._camera_ready.set()

        finally:
            if self.cap and self.cap.isOpened():
                self.cap.release()

            if self.show_window:
                try:
                    cv2.destroyAllWindows()
                except Exception:
                    pass

            logger.debug("Camera resources released.")

    def _run_inference(self, frame):
        """Preprocesses frame and runs TFLite inference.

        Args:
            frame: The input image frame.

        Returns:
            tuple[str, float]: The predicted label and its confidence score.
        """
        resized = cv2.resize(frame, (self.input_w, self.input_h))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

        top_idx = 0
        score = 0.0

        if self.backend_type == "tflite":
            input_data = np.expand_dims(rgb, axis=0)

            if not self.is_quantized:
                input_data = (input_data.astype(np.float32) / 127.5) - 1.0

            self.interpreter.set_tensor(self.input_idx, input_data)
            self.interpreter.invoke()
            output_data = self.interpreter.get_tensor(self.output_idx)[0].flatten()

            top_idx = np.argmax(output_data)

            scale, zero_point = self.quant_params
            if scale > 0:
                score = float(output_data[top_idx] - zero_point) * scale
            else:
                raw = float(output_data[top_idx])
                score = (raw / 255.0) if raw > 1.0 else raw

        label = self.labels[top_idx] if top_idx < len(self.labels) else f"Class {top_idx}"
        return label, score

    def _draw_debug(self, frame, label, score):
        """Draws bounding boxes and labels on the debug window."""
        try:
            color = (0, 255, 0) if score > self.threshold else (0, 0, 255)
            text = f"{label}: {score:.2f}"
            cv2.putText(frame, text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
            cv2.imshow("Loc.ai Vision", frame)
        except Exception:
            pass

    def _process_event_state(self, label, score):
        """Updates event state machine and detects stable events.

        Args:
            label: Detected label.
            score: Confidence score.

        Returns:
            dict | None: The event payload if an event is finalised.
        """
        now = time.time()
        # print(f"DEBUG: Processing event state: {label}, {score}")
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
        """Formats the final event payload.

        Returns:
             dict | None: The formatted event or None.
        """
        if not self.current_event:
            return None
        duration = self.current_event["end_ts"] - self.current_event["start_ts"]
        interval = time.time() - self.last_event_time
        if duration >= self.min_duration and interval >= self.min_interval:
            self.last_event_time = time.time()
            return {
                "model_id": self.model_id,
                "model_type": "classification",
                "sub_model_type": "image_classification",
                "model_output_type": "text",
                "model_output": self.current_event["label"],
                "model_output_confidence": round(self.current_event["max_conf"], 4),
                "model_output_start_time": datetime.fromtimestamp(self.current_event["start_ts"]).isoformat(),
                "model_output_end_time": datetime.fromtimestamp(self.current_event["end_ts"]).isoformat(),
                "model_output_duration": round(duration, 2),
                "model_output_metadata": {"source": "live_camera"},
            }
        return None
