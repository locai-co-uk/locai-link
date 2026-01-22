# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import argparse
import os
import queue
import signal
import sys
import threading
from datetime import datetime

import llama_cpp
import requests
from colorama import Fore, Style
from llama_cpp.llama_types import (
    ChatCompletionRequestAssistantMessage,
    ChatCompletionRequestSystemMessage,
    ChatCompletionRequestUserMessage,
)

# --- Global state ---
keep_running = True
worker_running = True
task_queue = queue.Queue()

# Get BASE_URL from environment (passed by agent)
BASE_URL = os.environ.get("BASE_URL")


def signal_handler(signum, frame):
    """Gracefully handle termination signals.

    Args:
        signum (int): The signal number.
        frame (traceback): The current stack frame.
    """
    global keep_running, worker_running
    print(f"\nTermination signal {signum} received. Shutting down...")
    sys.stdout.flush()
    keep_running = False
    worker_running = False
    # If waiting on input(), we might need to force exit or hit enter.
    # But usually signal interrupts input() call or loop.
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
    """Add a task to the background processing queue.

    Args:
        task_func (callable): The function to execute.
        *args: Arguments to pass to the function.
    """
    task_queue.put((task_func, args))


def send_telemetry_to_backend(metadata, device_id, api_key, model_id):
    """Sends telemetry data (token usage, timing) to the backend.

    Args:
        metadata (dict): Metadata about the model output.
        device_id (str): The ID of the device.
        api_key (str): The API key for authentication.
        model_id (str): The ID of the model.
    """
    if not BASE_URL:
        # If no URL, just skip silently or log once
        return

    payload = {
        "model_id": model_id,
        "model_type": "generation",
        "sub_model_type": "text_generation",
        "model_output_type": "telemetry",
        "model_output": "stats_only",  # Privacy: no content
        "model_output_confidence": 1.0,  # Placeholder
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
            print(
                f"[Telemetry] Error sending stats: {response.status_code} - {response.text}",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"[Telemetry] Failed to send stats: {e}", file=sys.stderr)


def main():
    """Main entry point with setup and cleanup."""
    global keep_running, worker_running

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(description="Run language model inference (GGUF) in interactive mode.")
    parser.add_argument("--model", required=True, help="Path to GGUF model file")
    parser.add_argument("--device-id", required=True, help="Device ID")
    parser.add_argument("--api-key", required=True, help="API key")

    # Model Load Params
    parser.add_argument("--n-ctx", type=int, default=2048, help="Context window size")
    parser.add_argument(
        "--n-gpu-layers",
        type=int,
        default=35,
        help="Number of layers to offload to GPU",
    )

    # Generation Params
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-tokens", type=int, default=2000)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--top-k", type=int, default=40)
    parser.add_argument("--repeat-penalty", type=float, default=1.1)

    # Config
    parser.add_argument("--system-prompt", default="You are a helpful assistant.")
    parser.add_argument("--stream", action="store_true", default=True, help="Stream output to stdout")
    parser.add_argument("--no-stream", dest="stream", action="store_false", help="Disable streaming")

    # Unused but captured to prevent wrapper connection errors
    parser.add_argument("--camera-index", help=argparse.SUPPRESS)
    parser.add_argument("--width", help=argparse.SUPPRESS)
    parser.add_argument("--height", help=argparse.SUPPRESS)
    parser.add_argument("--fps", help=argparse.SUPPRESS)
    parser.add_argument("--sample-rate", help=argparse.SUPPRESS)
    parser.add_argument("--channels", help=argparse.SUPPRESS)
    parser.add_argument("--confidence-threshold", help=argparse.SUPPRESS)
    parser.add_argument("--min-event-duration", help=argparse.SUPPRESS)
    parser.add_argument("--min-event-interval", help=argparse.SUPPRESS)

    args, unknown = parser.parse_known_args()

    if not os.path.exists(args.model):
        print(f"Error: Model file not found at {args.model}", file=sys.stderr)
        sys.exit(1)

    # Start background thread
    io_thread = threading.Thread(target=io_worker, daemon=True)
    io_thread.start()

    print(f"Loading model: {args.model}...")
    try:
        llm = llama_cpp.Llama(
            model_path=args.model,
            n_ctx=args.n_ctx,
            n_gpu_layers=args.n_gpu_layers,
            verbose=False,
        )
    except Exception as e:
        print(f"Error loading model: {e}", file=sys.stderr)
        sys.exit(1)

    print("Model loaded. ready for input (type 'quit' to exit).")
    print(f"System Prompt: {args.system_prompt}")
    sys.stdout.flush()

    messages = [ChatCompletionRequestSystemMessage(role="system", content=args.system_prompt)]

    model_id = os.path.basename(args.model)  # simplistic ID from filename

    while keep_running:
        try:
            # Simple blocking input.
            # In a production agent pipe scenario, this reads line by line from pipe buffer.
            user_input = input(f"{Fore.GREEN}\nUser: {Style.RESET_ALL}")

            if user_input.lower() in ("quit", "exit"):
                break

            messages.append(ChatCompletionRequestUserMessage(role="user", content=user_input))

            start_time = datetime.now()
            start_ts = start_time.isoformat()

            print(f"{Fore.BLUE}Assistant: {Style.RESET_ALL}", end="", flush=True)

            # Record metrics
            tokens_generated = 0
            full_response_text = ""

            # Determine mode
            try:
                if args.stream:
                    stream_completion = llm.create_chat_completion(
                        messages=messages,
                        max_tokens=args.max_tokens,
                        temperature=args.temperature,
                        top_p=args.top_p,
                        top_k=args.top_k,
                        repeat_penalty=args.repeat_penalty,
                        stream=True,
                    )

                    for chunk in stream_completion:
                        # chunk is Dict[str, Any]
                        delta = chunk["choices"][0]["delta"]
                        if "content" in delta:
                            content = delta["content"]
                            print(content, end="", flush=True)
                            full_response_text += content
                            tokens_generated += 1
                else:
                    completion = llm.create_chat_completion(
                        messages=messages,
                        max_tokens=args.max_tokens,
                        temperature=args.temperature,
                        top_p=args.top_p,
                        top_k=args.top_k,
                        repeat_penalty=args.repeat_penalty,
                        stream=False,
                    )
                    # completion is Dict[str, Any]
                    full_response_text = completion["choices"][0]["message"]["content"]
                    tokens_generated = completion["usage"]["completion_tokens"]  # Accurate
                    print(full_response_text, end="", flush=True)

            except Exception as e:
                print(f"\n[Error during generation]: {e}", file=sys.stderr)
                continue

            print("")  # Newline at end
            sys.stdout.flush()

            # End of turn logic
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            # Approximate prompt tokens if not available (llama-cpp-python usage in stream is tricky)
            # For non-stream it gives usage. For stream we count output manualy.
            # We can use llm.tokenize() to count inputs if strict accuracy needed
            # For now, let's just use what we have.

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
            messages.append(ChatCompletionRequestAssistantMessage(role="assistant", content=full_response_text))

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
