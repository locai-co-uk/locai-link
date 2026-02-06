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

import requests
from colorama import Fore, Style

# --- Global state ---
keep_running = True
worker_running = True
task_queue = queue.Queue()

# Get BASE_URL from environment (passed by agent)
BASE_URL = os.environ.get("BASE_URL")


def signal_handler(signum, frame):
    """Gracefully handle termination signals."""
    global keep_running, worker_running
    print(f"\nTermination signal {signum} received. Shutting down...")
    sys.stdout.flush()
    keep_running = False
    worker_running = False
    sys.exit(0)


def io_worker():
    """Background worker that processes I/O tasks from the queue."""
    while worker_running:
        try:
            task, args = task_queue.get(timeout=0.5)
            try:
                task(*args)
                task_queue.task_done()
            except Exception as e:
                print(f"[ERROR in io_worker] {e}", file=sys.stderr)
        except queue.Empty:
            pass


def queue_task(task_func, *args):
    """Add a task to the background processing queue."""
    task_queue.put((task_func, args))


def send_telemetry_to_backend(metadata, device_id, api_key, model_id):
    """Sends telemetry data (token usage, timing) to the backend."""
    if not BASE_URL:
        return

    payload = {
        "model_id": model_id,
        "model_type": "generation",
        "sub_model_type": "text_generation",
        "model_output_type": "telemetry",
        "model_output": "stats_only",
        "model_output_confidence": 1.0,
        "model_output_start_time": metadata.get("start_time"),
        "model_output_end_time": metadata.get("end_time"),
        "model_output_duration": metadata.get("duration"),
        "model_output_metadata": metadata,
    }

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = f"{BASE_URL}/agent/model_results/{device_id}/create_from_agent"

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"[Telemetry] Error sending stats: {response.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"[Telemetry] Failed to send stats: {e}", file=sys.stderr)


def wait_for_server(url, timeout=30):
    """Waits for the inference server to be ready."""
    start_t = time.time()
    print(f"Connecting to Inference Server at {url}...", end="", flush=True)
    while time.time() - start_t < timeout:
        try:
            # The /health endpoint is standard on llama-server
            requests.get(f"{url}/health", timeout=1)
            print(" Connected.")
            return True
        except requests.exceptions.RequestException:
            time.sleep(1)
            print(".", end="", flush=True)
    print("\nError: Inference Server connection timed out.")
    return False


def main():
    """Main entry point acting as a Client to the Model Server."""
    global keep_running, worker_running

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(description="Run language model client.")
    # Note: We no longer need model path here strictly, but we keep it for ID purposes
    parser.add_argument("--model", required=True, help="Model ID or Path (used for logging)")
    parser.add_argument("--device-id", required=True, help="Device ID")
    parser.add_argument("--api-key", required=True, help="API key")

    # Connection Params
    parser.add_argument("--server-port", type=int, default=8003, help="Port of the running llama-server")
    parser.add_argument("--server-host", type=str, default="localhost", help="Host of the running llama-server")

    # Generation Params
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-tokens", type=int, default=2000)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--repeat-penalty", type=float, default=1.1)

    # Config
    parser.add_argument("--system-prompt", default="You are a helpful assistant.")
    parser.add_argument("--stream", action="store_true", default=True)

    args, unknown = parser.parse_known_args()

    # Start background thread for telemetry
    io_thread = threading.Thread(target=io_worker, daemon=True)
    io_thread.start()

    server_url = f"http://{args.server_host}:{args.server_port}"

    # Wait for the separate server process (managed by server.py) to come online
    if not wait_for_server(server_url):
        sys.exit(1)

    print("Client ready for input (type 'quit' to exit).")
    print(f"System Prompt: {args.system_prompt}")
    sys.stdout.flush()

    messages = [{"role": "system", "content": args.system_prompt}]
    model_id = os.path.basename(args.model)

    chat_endpoint = f"{server_url}/v1/chat/completions"

    while keep_running:
        try:
            user_input = input(f"{Fore.GREEN}\nUser: {Style.RESET_ALL}")

            if user_input.lower() in ("quit", "exit"):
                break

            messages.append({"role": "user", "content": user_input})

            start_time = datetime.now()
            start_ts = start_time.isoformat()

            print(f"{Fore.BLUE}Assistant: {Style.RESET_ALL}", end="", flush=True)

            payload = {
                "messages": messages,
                "max_tokens": args.max_tokens,
                "temperature": args.temperature,
                "top_p": args.top_p,
                "frequency_penalty": 0.0,  # mapped roughly to repeat_penalty in some versions
                "stream": args.stream,
            }

            tokens_generated = 0
            full_response_text = ""

            try:
                response = requests.post(chat_endpoint, json=payload, stream=args.stream, timeout=600)
                response.raise_for_status()

                if args.stream:
                    for line in response.iter_lines():
                        if not line:
                            continue

                        line_text = line.decode("utf-8")
                        if line_text.startswith("data: "):
                            data_str = line_text[6:]  # Strip "data: "

                            if data_str.strip() == "[DONE]":
                                break

                            try:
                                chunk = json.loads(data_str)
                                if "choices" in chunk and len(chunk["choices"]) > 0:
                                    delta = chunk["choices"][0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        print(content, end="", flush=True)
                                        full_response_text += content
                                        tokens_generated += 1
                            except json.JSONDecodeError:
                                pass
                else:
                    # Non-stream mode
                    data = response.json()
                    full_response_text = data["choices"][0]["message"]["content"]
                    tokens_generated = data["usage"].get("completion_tokens", 0)
                    print(full_response_text, end="", flush=True)

            except Exception as e:
                print(f"\n[Error during generation]: {e}", file=sys.stderr)
                # Don't add failed assistant message to history
                continue

            print("")  # Newline
            sys.stdout.flush()

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            metadata = {
                "start_time": start_ts,
                "end_time": end_time.isoformat(),
                "duration": duration,
                "tokens_generated": tokens_generated,
                "temperature": args.temperature,
            }

            # Send telemetry
            queue_task(
                send_telemetry_to_backend,
                metadata,
                args.device_id,
                args.api_key,
                model_id,
            )

            # Update history
            messages.append({"role": "assistant", "content": full_response_text})

        except (EOFError, KeyboardInterrupt):
            break
        except Exception as e:
            print(f"Error in conversation loop: {e}", file=sys.stderr)
            break

    # Cleanup
    worker_running = False
    io_thread.join(timeout=1.0)
    print("Exiting...")


if __name__ == "__main__":
    main()
