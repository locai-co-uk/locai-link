# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import argparse
import csv
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
import tensorflow as tf
from rich import print

from link.analytics import send_model_ready

# --- Global state for signal handling and detection throttling ---
keep_running = True
last_detection_time = 0  # Track the last time we saved/sent a detection
MIN_DETECTION_INTERVAL = 0.5  # Default minimum time (in seconds) between detections (can be overridden)
MIN_EVENT_DURATION = 1.0  # Default minimum event duration in seconds (can be overridden)

# Create a task queue for background operations
task_queue = queue.Queue()
worker_running = True

"""Default YAMNet parameters; can be overridden via CLI."""
SAMPLE_RATE_DEFAULT = 16000  # 16 kHz sample rate for YAMNet
PATCH_WINDOW_SECONDS = 0.975  # Minimum window for YAMNet (975ms)
PATCH_HOP_SECONDS = 0.48  # Hop size for overlapping windows


# Worker thread function to process tasks in the background
def io_worker():
    """Background worker that processes I/O tasks from the queue."""
    while worker_running:
        try:
            # Get a task with a timeout to allow checking worker_running
            task, args = task_queue.get(timeout=0.5)
            try:
                task(*args)
                # Mark the task as done
                task_queue.task_done()
                # Flush stdout to ensure output is visible
                sys.stdout.flush()
            except Exception as e:
                print(f"[ERROR in io_worker] {e}")
                sys.stdout.flush()
        except queue.Empty:
            # No tasks in queue, just continue
            pass


# Get BASE_URL from environment (passed by agent)
# The agent is responsible for reading .env and constructing BASE_URL
BASE_URL = os.environ.get("BASE_URL")

if BASE_URL:
    print(f"Using API URL: {BASE_URL}")
else:
    print("Warning: BASE_URL not configured. Detection results will not be sent to backend.")
    print("Note: BASE_URL should be passed by the agent when starting inference.")


def signal_handler(signum, frame):
    """Gracefully handle termination signals (like SIGTERM from the agent)."""
    global keep_running, worker_running
    print(f"Termination signal {signum} received. Shutting down...")
    sys.stdout.flush()
    keep_running = False
    worker_running = False


def load_yamnet_labels():
    """Load YAMNet class labels from CSV file or download from web."""
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    models_dir = project_root / "models"
    models_dir.mkdir(exist_ok=True)
    labels_path = models_dir / "yamnet_class_map.csv"

    if os.path.exists(labels_path):
        try:
            class_names = []
            with open(labels_path, "r") as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    class_names.append(row["display_name"])
            return class_names
        except Exception as e:
            print(f"Error reading local YAMNet labels: {e}")

    try:
        # Download YAMNet class map from TensorFlow Hub
        url = "https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv"
        response = requests.get(url)

        if response.status_code == 200:
            # Save to local file for future use
            with open(labels_path, "w") as f:
                f.write(response.text)

            # Parse the CSV content
            class_names = []
            lines = response.text.strip().split("\n")
            for line in lines[1:]:  # Skip header
                parts = line.split(",")
                if len(parts) >= 3:
                    class_names.append(parts[2])  # display_name is the third column

            return class_names
        else:
            print(f"Failed to download YAMNet labels: {response.status_code}")
    except Exception as e:
        print(f"Error downloading YAMNet labels: {e}")

    # Fallback: create dummy labels
    return [f"Audio_Class_{i}" for i in range(521)]


def save_detection(detection, results_dir):
    """This function is now a no-op - we no longer save files locally to improve performance.

    It's kept as a placeholder to maintain compatibility with existing code.

    Args:
        detection (dict): The detection to save.
        results_dir (str): The directory to save the detection to.
    """
    # Just log that we would have saved a file
    print(f"Local file saving disabled - would have saved detection for '{detection['label']}'")
    sys.stdout.flush()
    return


def send_detection_to_backend(detection, device_id, api_key, model_id, sample_rate) -> bool:
    """Sends a single detection result to the backend API.

    Args:
        detection (dict): The detection to send.
        device_id (str): The device ID.
        api_key (str): The API key.
        model_id (str): The model ID.
        sample_rate (int): The sample rate.

    Returns:
        bool: True if the detection was sent successfully, False otherwise.
    """
    if not BASE_URL:
        print("Error: API URL not configured. Cannot send detection to backend.")
        sys.stdout.flush()
        return False

    # Reduced debug logging to avoid console flooding
    start_time = datetime.fromisoformat(detection["start_time"])
    end_time = datetime.fromisoformat(detection["end_time"])
    duration = (end_time - start_time).total_seconds()

    # Skip detections that are too brief (configurable minimum duration)
    # Note: This function doesn't have access to min_duration, so we keep the default check
    # The actual filtering happens in the audio_thread before calling this function
    if duration < 0.5:  # Basic sanity check
        print(f"Skipping detection for '{detection['label']}' - duration ({duration:.2f}s) is too brief")
        sys.stdout.flush()
        return False

    payload = {
        "model_id": model_id,
        "model_type": "classification",
        "sub_model_type": "audio_classification",
        "model_output_type": "text",
        "model_output": detection["label"],
        "model_output_confidence": detection["confidence"],
        "model_output_start_time": detection["start_time"],
        "model_output_end_time": detection["end_time"],
        "model_output_duration": duration,
        "model_output_metadata": {
            "source": "live_microphone",
            "sample_rate": sample_rate,
            "window_seconds": PATCH_WINDOW_SECONDS,
        },
    }

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    url = f"{BASE_URL}/agent/model_results/{device_id}/create_from_agent"

    try:
        print(f"Sending detection for '{detection['label']}' to backend...")
        sys.stdout.flush()
        response = requests.post(url, json=payload, headers=headers)

        if response.status_code == 200:
            print(f"Successfully sent detection for '{detection['label']}' to backend.")
            sys.stdout.flush()
            return True
        else:
            print(f"Error sending detection: {response.status_code} - {response.text}")
            sys.stdout.flush()
            return False
    except requests.exceptions.RequestException as e:
        print(f"Failed to send detection to backend: {e}")
        sys.stdout.flush()
        return False


# Function to queue a task for background processing
def queue_task(task_func, *args):
    """Add a task to the background processing queue.

    Args:
        task_func (function): The function to queue.
        *args: The arguments to pass to the function.
    """
    task_queue.put((task_func, args))


def audio_thread(
    model_path,
    audio_queue,
    device_id,
    api_key,
    sample_rate=SAMPLE_RATE_DEFAULT,
    channels=1,
    confidence_threshold=0.3,
    min_event_duration=1.0,
    min_event_interval=0.5,
):
    """Thread for capturing audio and running inference.

    Args:
        model_path (str): The path to the TFLite model.
        audio_queue (queue.Queue): The queue for audio data.
        device_id (str): The device ID.
        api_key (str): The API key.
        sample_rate (int): The sample rate.
        channels (int): The number of channels.
        confidence_threshold (float): The confidence threshold.
        min_event_duration (float): The minimum event duration.
        min_event_interval (float): The minimum event interval.
    """
    global keep_running, last_detection_time, worker_running

    # Use the provided parameters or defaults
    min_duration = float(min_event_duration)
    min_interval = float(min_event_interval)

    # --- Setup ---
    labels = load_yamnet_labels()
    print(f"Loaded {len(labels)} YAMNet class labels")

    if not os.path.exists(model_path):
        print(f"Error: Model file not found at {model_path}")
        return

    try:
        interpreter = tf.lite.Interpreter(model_path=model_path)
        interpreter.allocate_tensors()
    except Exception as e:
        print(f"Error loading model: {e}")
        return

    if device_id and api_key:
        try:
            model_id = Path(model_path).stem
            send_model_ready(
                base_url=BASE_URL,
                device_id=device_id,
                api_key=api_key,
                model_id=model_id,
                model_name=Path(model_path).name,
                mode="inference",
                runner="tflite_audio_classification",
                model_format="tflite",
            )
        except Exception:
            pass

    input_details = interpreter.get_input_details()
    output_details = interpreter.get_output_details()

    print(f"Model input shape: {input_details[0]['shape']}")
    print(f"Model output shape: {output_details[0]['shape']}")

    # Audio buffer for continuous capture
    waveform_length = int(sample_rate * PATCH_WINDOW_SECONDS)
    audio_buffer = np.zeros(waveform_length, dtype=np.float32)
    buffer_index = 0

    # --- Detection Logging State ---
    current_detection = None
    last_label = None

    def audio_callback(indata, frames, time, status):
        """Callback function for audio stream.

        Args:
            indata (np.ndarray): Input audio data.
            frames (int): Number of frames.
            time (tuple): Time information.
            status (str): Status information.
        """
        nonlocal audio_buffer, buffer_index

        if status:
            print(f"Audio callback status: {status}")

        # Convert to mono if stereo and flatten to 1D
        if len(indata.shape) > 1 and indata.shape[1] > 1:
            audio_data = np.mean(indata, axis=1).flatten()
        else:
            audio_data = indata.flatten()

        # Add to circular buffer
        for sample in audio_data:
            audio_buffer[buffer_index] = sample
            buffer_index = (buffer_index + 1) % waveform_length

    # Start audio stream
    try:
        with sd.InputStream(
            callback=audio_callback,
            channels=int(channels),
            samplerate=int(sample_rate),
            blocksize=int(sample_rate * 0.1),  # 100ms blocks
            dtype="float32",
        ):
            print(f"Started audio capture at {sample_rate} Hz, channels={channels}")
            sys.stdout.flush()

            # Allow some time for the buffer to fill
            time.sleep(1.0)

            # --- Main Loop ---
            while keep_running:
                try:
                    # Get current audio window
                    current_window = np.roll(audio_buffer, -buffer_index)

                    # Normalize to [-1.0, 1.0] range as expected by YAMNet
                    current_window = np.clip(current_window, -1.0, 1.0)

                    # Ensure current_window is 1D and prepare input for TFLite model
                    current_window = current_window.flatten()
                    input_data = current_window.astype(np.float32)

                    # Verify input shape matches model expectation
                    if input_data.shape != (waveform_length,):
                        print(f"Warning: Input shape {input_data.shape} doesn't match expected {(waveform_length,)}")
                        continue

                    # Run inference
                    interpreter.set_tensor(input_details[0]["index"], input_data)
                    interpreter.invoke()
                    output_data = interpreter.get_tensor(output_details[0]["index"])

                    # Get top prediction
                    top_prediction_index = np.argmax(output_data[0])
                    confidence = float(output_data[0][top_prediction_index])
                    label = labels[top_prediction_index] if top_prediction_index < len(labels) else "Unknown"

                    # --- Log Detection Logic ---
                    current_time = time.time()
                    time_since_last_detection = current_time - last_detection_time

                    # Confidence threshold for audio classification (lower than image)
                    if confidence >= float(confidence_threshold):
                        if label != last_label:
                            # End the previous detection if there was one
                            if current_detection:
                                current_detection["end_time"] = datetime.now().isoformat()

                                # Calculate duration
                                start_time = datetime.fromisoformat(current_detection["start_time"])
                                end_time = datetime.fromisoformat(current_detection["end_time"])
                                duration = (end_time - start_time).total_seconds()

                                # Only process if duration meets minimum threshold
                                if duration >= min_duration:
                                    # Only save/send if enough time has passed since the last detection
                                    if time_since_last_detection >= min_interval:
                                        # Create a copy of the detection data for thread safety
                                        detection_copy = current_detection.copy()

                                        # Add API task to queue
                                        if device_id and api_key:
                                            model_id = Path(model_path).stem
                                            queue_task(
                                                send_detection_to_backend,
                                                detection_copy,
                                                device_id,
                                                api_key,
                                                model_id,
                                                sample_rate,
                                            )

                                        # Update the last detection time
                                        last_detection_time = current_time

                            # Start a new detection
                            current_detection = {
                                "label": label,
                                "confidence": float(confidence),
                                "start_time": datetime.now().isoformat(),
                                "end_time": None,
                            }
                            last_label = label
                    else:
                        # If confidence drops, end the current detection
                        if current_detection:
                            current_detection["end_time"] = datetime.now().isoformat()

                            # Calculate duration and handle if necessary
                            duration = (
                                datetime.fromisoformat(current_detection["end_time"])
                                - datetime.fromisoformat(current_detection["start_time"])
                            ).total_seconds()
                            if duration >= min_duration and time_since_last_detection >= min_interval:
                                detection_copy = current_detection.copy()
                                if device_id and api_key:
                                    model_id = Path(model_path).stem
                                    queue_task(
                                        send_detection_to_backend,
                                        detection_copy,
                                        device_id,
                                        api_key,
                                        model_id,
                                        sample_rate,
                                    )
                                last_detection_time = time.time()

                            current_detection = None
                        last_label = None

                    # Put the processed result into the queue for display
                    if not audio_queue.full():
                        audio_queue.put((label, confidence))

                    # Sleep to control inference rate
                    time.sleep(PATCH_HOP_SECONDS)

                except Exception as e:
                    print(f"Error during audio loop: {e}")
                    sys.stdout.flush()

    except Exception as e:
        print(f"Error starting audio stream: {e}")
        sys.stdout.flush()
        return

    # --- Cleanup ---
    print("Audio thread has finished.")
    sys.stdout.flush()


def main():
    """Main entry point with setup and cleanup."""
    global worker_running, keep_running

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    parser = argparse.ArgumentParser(description="Run live audio classification using YAMNet and log results.")
    parser.add_argument("--model", required=True, help="Path to the YAMNet TFLite model file.")
    parser.add_argument("--device-id", help="Device ID for sending results to backend API")
    parser.add_argument("--api-key", help="API key for authentication with backend API")
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=SAMPLE_RATE_DEFAULT,
        help="Microphone sample rate (Hz)",
    )
    parser.add_argument("--channels", type=int, default=1, help="Number of microphone channels")
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.3,
        help="Confidence threshold for detections",
    )
    parser.add_argument(
        "--min-event-duration",
        type=float,
        default=MIN_EVENT_DURATION,
        help="Minimum event duration in seconds",
    )
    parser.add_argument(
        "--min-event-interval",
        type=float,
        default=MIN_DETECTION_INTERVAL,
        help="Minimum interval between events in seconds",
    )
    args = parser.parse_args()

    # Create a queue for passing results from audio thread to main thread
    audio_queue = queue.Queue(maxsize=1)

    # Start the worker thread for background I/O tasks
    print("Starting background I/O worker thread...")
    sys.stdout.flush()
    io_thread = threading.Thread(target=io_worker, daemon=True)
    io_thread.start()

    # Start the audio capture and inference thread
    print("Starting audio capture and inference thread...")
    print(
        f"Configuration: confidence_threshold={args.confidence_threshold}, "
        f"min_event_duration={args.min_event_duration}s, "
        f"min_event_interval={args.min_event_interval}s"
    )
    sys.stdout.flush()
    audio_thread_obj = threading.Thread(
        target=audio_thread,
        args=(
            args.model,
            audio_queue,
            args.device_id,
            args.api_key,
            args.sample_rate,
            args.channels,
            args.confidence_threshold,
            args.min_event_duration,
            args.min_event_interval,
        ),
        daemon=True,
    )
    audio_thread_obj.start()

    try:
        while keep_running and audio_thread_obj.is_alive():
            try:
                # Get the latest results from the queue
                label, confidence = audio_queue.get(timeout=1.0)

                # --- Display Results ---
                display_text = f"Audio: {label} ({confidence:.3f})"
                print(f"\r{display_text}", end="", flush=True)

            except queue.Empty:
                # If queue is empty, check if audio thread is still running
                if not audio_thread_obj.is_alive():
                    print("\nAudio thread has stopped, exiting...")
                    sys.stdout.flush()
                    break
                else:
                    print("\rWaiting for audio...", end="", flush=True)
    except KeyboardInterrupt:
        print("\nInterrupted by user, shutting down...")
        sys.stdout.flush()
    finally:
        # Signal all threads to stop
        keep_running = False
        worker_running = False

        print("\nShutting down...")
        sys.stdout.flush()

        # Wait for threads to finish
        audio_thread_obj.join(timeout=2.0)

        # Wait for all I/O tasks to complete
        task_queue.join()

        io_thread.join(timeout=2.0)

        print("Cleanup complete.")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
