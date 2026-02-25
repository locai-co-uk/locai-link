# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests
import tensorflow as tf
from PIL import Image
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


def load_imagenet_labels() -> list[str]:
    """Load ImageNet labels from the web or use a cached version.

    Returns:
        list[str]: A list of ImageNet labels.
    """
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    models_dir = project_root / "models"
    models_dir.mkdir(exist_ok=True)
    labels_path = models_dir / "imagenet_labels.json"

    if os.path.exists(labels_path):
        with open(labels_path, "r") as f:
            return json.load(f)

    try:
        url = "https://storage.googleapis.com/download.tensorflow.org/data/ImageNetLabels.txt"
        response = requests.get(url)
        labels = response.text.strip().split("\n")

        with open(labels_path, "w") as f:
            json.dump(labels, f)

        return labels
    except Exception as e:
        print(f"Error loading ImageNet labels: {e}")
        return ["Unknown"] * 1001


def save_detection(detection, results_dir):
    """This function is now a no-op - we no longer save files locally to improve performance.

    It's kept as a placeholder to maintain compatibility with existing code.

    Args:
        detection (dict): The detection result.
        results_dir (str): The directory to save the detection to.
    """
    # Just log that we would have saved a file
    print(f"Local file saving disabled - would have saved detection for '{detection['label']}'")
    sys.stdout.flush()
    return


def send_detection_to_backend(detection, device_id, api_key, model_id) -> bool:
    """Sends a single detection result to the backend API.

    Args:
        detection (dict): The detection result.
        device_id (str): The ID of the device.
        api_key (str): The API key for authentication.
        model_id (str): The ID of the model.
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
    # The actual filtering happens in the camera_thread before calling this function
    if duration < 0.5:  # Basic sanity check
        print(f"Skipping detection for '{detection['label']}' - duration ({duration:.2f}s) is too brief")
        sys.stdout.flush()
        return False

    payload = {
        "model_id": model_id,
        "model_type": "classification",
        "sub_model_type": "image_classification",
        "model_output_type": "text",
        "model_output": detection["label"],
        "model_output_confidence": detection["confidence"],
        "model_output_start_time": detection["start_time"],
        "model_output_end_time": detection["end_time"],
        "model_output_duration": duration,
        "model_output_metadata": {"source": "live_camera"},
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
    """Add a task to the background processing queue."""
    task_queue.put((task_func, args))


def camera_thread(
    model_path,
    frame_queue,
    device_id,
    api_key,
    camera_index=0,
    width_override=None,
    height_override=None,
    fps_override=None,
    confidence_threshold=0.5,
    min_event_duration=1.0,
    min_event_interval=0.5,
):
    """Thread for capturing frames and running inference.

    Args:
        model_path (str): The path to the model file.
        frame_queue (queue.Queue): The queue to put frames in.
        device_id (str): The ID of the device.
        api_key (str): The API key for authentication.
        camera_index (int): The index of the camera.
        width_override (int): The width override.
        height_override (int): The height override.
        fps_override (int): The fps override.
        confidence_threshold (float): The confidence threshold.
        min_event_duration (float): The minimum event duration.
        min_event_interval (float): The minimum event interval.
    """
    global keep_running, last_detection_time, worker_running

    # Use the provided parameters or defaults
    min_duration = float(min_event_duration)
    min_interval = float(min_event_interval)

    # --- Setup ---
    labels = load_imagenet_labels()

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
                runner="tflite_image_detection",
                model_format="tflite",
            )
        except Exception:
            pass

    input_details = interpreter.get_input_details()
    output_details = interpreter.get_output_details()

    cap = cv2.VideoCapture(int(camera_index))
    if not cap.isOpened():
        print("Error: Cannot open camera")
        return

    # Get model's expected input dimensions (these cannot be changed)
    input_shape = input_details[0]["shape"]
    model_height, model_width = input_shape[1], input_shape[2]

    print(f"Model expects input size: {model_width}x{model_height}")

    # Apply camera capture resolution overrides if provided (for capture only, not model input)
    capture_width = width_override if width_override else None
    capture_height = height_override if height_override else None

    if capture_width and capture_height:
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(capture_width))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(capture_height))
            print(f"Camera capture resolution set to: {capture_width}x{capture_height}")
        except Exception as e:
            print(f"Warning: Could not set camera resolution: {e}")

    if fps_override:
        try:
            cap.set(cv2.CAP_PROP_FPS, float(fps_override))
            print(f"Camera FPS set to: {fps_override}")
        except Exception as e:
            print(f"Warning: Could not set camera FPS: {e}")

    # --- Detection Logging State ---
    current_detection = None
    last_label = None

    # --- Main Loop ---
    while keep_running:
        ret, frame = cap.read()
        if not ret:
            print("Error reading frame from camera")
            break

        try:
            # Process the frame for inference
            # Always resize to model's expected input dimensions (not camera resolution)
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb_frame).resize((model_width, model_height))
            input_data = np.expand_dims(pil_img, axis=0).astype(np.uint8)

            interpreter.set_tensor(input_details[0]["index"], input_data)
            interpreter.invoke()
            output_data = interpreter.get_tensor(output_details[0]["index"])

            top_prediction_index = np.argmax(output_data[0])
            confidence = float(output_data[0][top_prediction_index]) * output_details[0]["quantization"][0]
            label = labels[top_prediction_index] if top_prediction_index < len(labels) else "Unknown"

            # --- Log Detection Logic ---
            current_time = time.time()
            time_since_last_detection = current_time - last_detection_time

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
                            )
                        last_detection_time = time.time()

                    current_detection = None
                last_label = None

            # Put the processed frame and result into the queue for display
            if not frame_queue.full():
                frame_queue.put((frame, label, confidence))

        except Exception as e:
            print(f"Error during camera loop: {e}")
            sys.stdout.flush()

    # --- Cleanup ---
    cap.release()
    print("Camera thread has finished.")
    sys.stdout.flush()


def main():
    """Main entry point with proper setup and cleanup."""
    global worker_running, keep_running

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    parser = argparse.ArgumentParser(description="Run live object detection and log results.")
    parser.add_argument("--model", required=True, help="Path to the TFLite model file.")
    parser.add_argument("--device-id", help="Device ID for sending results to backend API")
    parser.add_argument("--api-key", help="API key for authentication with backend API")
    parser.add_argument("--camera-index", type=int, default=0, help="Camera index to open (default: 0)")
    parser.add_argument("--width", type=int, help="Override capture width")
    parser.add_argument("--height", type=int, help="Override capture height")
    parser.add_argument("--fps", type=int, help="Override capture FPS")
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.5,
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

    # Create a queue for passing frames from camera thread to main thread
    frame_queue = queue.Queue(maxsize=1)

    # Start the worker thread for background I/O tasks
    print("Starting background I/O worker thread...")
    sys.stdout.flush()
    io_thread = threading.Thread(target=io_worker, daemon=True)
    io_thread.start()

    # Start the camera and inference thread
    print("Starting camera and inference thread...")
    print(
        f"Configuration: confidence_threshold={args.confidence_threshold}, "
        f"min_event_duration={args.min_event_duration}s, "
        f"min_event_interval={args.min_event_interval}s"
    )
    sys.stdout.flush()
    cam_thread = threading.Thread(
        target=camera_thread,
        args=(
            args.model,
            frame_queue,
            args.device_id,
            args.api_key,
            args.camera_index,
            args.width,
            args.height,
            args.fps,
            args.confidence_threshold,
            args.min_event_duration,
            args.min_event_interval,
        ),
        daemon=True,
    )
    cam_thread.start()

    window_created = False
    try:
        while keep_running and cam_thread.is_alive():
            try:
                # Get the latest frame and results from the queue
                # Reduced timeout to keep UI responsive
                frame, label, confidence = frame_queue.get(timeout=0.1)

                # --- Display on Frame ---
                display_text = f"{label} ({confidence:.2f})"
                cv2.putText(
                    frame,
                    display_text,
                    (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1,
                    (0, 255, 0),
                    2,
                    cv2.LINE_AA,
                )

                # Show the frame in a window
                cv2.imshow("Live Classification", frame)
                window_created = True

                # Check for 'q' key press to exit
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    print("User pressed 'q', exiting...")
                    sys.stdout.flush()
                    keep_running = False

                # Check if window was closed via X button
                if cv2.getWindowProperty("Live Classification", cv2.WND_PROP_VISIBLE) < 1:
                    print("Window closed by user, exiting...")
                    sys.stdout.flush()
                    keep_running = False
            except queue.Empty:
                # Keep UI responsive even if no new frames
                if window_created:
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        print("User pressed 'q', exiting...")
                        sys.stdout.flush()
                        keep_running = False

                    if cv2.getWindowProperty("Live Classification", cv2.WND_PROP_VISIBLE) < 1:
                        print("Window closed by user, exiting...")
                        sys.stdout.flush()
                        keep_running = False

                # If queue is empty, check if camera thread is still running
                if not cam_thread.is_alive():
                    print("Camera thread has stopped, exiting...")
                    sys.stdout.flush()
                    break
                else:
                    # Optional: Print explicit waiting message only occasionally to avoid log spam
                    pass
    except KeyboardInterrupt:
        print("Interrupted by user, shutting down...")
        sys.stdout.flush()
    finally:
        # Signal all threads to stop
        keep_running = False
        worker_running = False

        print("Shutting down...")
        sys.stdout.flush()

        # Wait for threads to finish
        cam_thread.join(timeout=2.0)

        # Wait for all I/O tasks to complete
        task_queue.join()

        io_thread.join(timeout=2.0)

        cv2.destroyAllWindows()
        print("Cleanup complete.")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
