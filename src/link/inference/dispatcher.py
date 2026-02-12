# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from link.utils import CONFIGS_DIR, MODELS_DIR, load_json_config

AUDIO_KEYWORDS = ["yamnet", "audio", "sound", "speech", "voice"]
LLM_KEYWORDS = ["llama", "gguf", "mistral", "deepseek", "phi", "qwen", "language"]

child_process = None
llm_detached_mode = False  # Indicates if LLM was launched in a new terminal
llm_model_path = None  # Used to find detached LLM process

# This script is located in src/link/inference/
SCRIPT_DIR = Path(__file__).parent


def determine_inference_script_by_name(name: str) -> Path:
    """Return script path based on audio-vs-image keywords in the name."""
    lower = (name or "").lower()
    if any(k in lower for k in AUDIO_KEYWORDS):
        return SCRIPT_DIR / "audio_classification_yamnet_tflite.py"
    if any(k in lower for k in LLM_KEYWORDS):
        return SCRIPT_DIR / "language_model_gguf.py"
    # Default to image detection/classification script
    return SCRIPT_DIR / "image_detection_cpy_tflite.py"


def find_runtime_config_file(model_id: str) -> Path | None:
    """Find the runtime config file for the model."""
    # Ensure configs dir exists (using constant from utils)
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)

    # First check configs directory (preferred)
    config_file_path = CONFIGS_DIR / f"{model_id}.json"
    if config_file_path.exists():
        return config_file_path

    # Fallback to models directory (legacy)
    legacy_config_path = MODELS_DIR / f"{model_id}.json"
    if legacy_config_path.exists():
        print(f"Warning: Using legacy config location {legacy_config_path}")
        return legacy_config_path

    return None


def determine_inference_script_by_config(config: dict) -> Path:
    """Return script path based on the config file."""
    impl = (config.get("process") or {}).get("impl") or {}
    runner = impl.get("runner")
    entrypoint = impl.get("entrypoint")

    # Map runner to the physical filenames on disk
    runner_to_script_path = {
        "tflite_audio_classification": "audio_classification_yamnet_tflite.py",
        "tflite_image_detection": "image_detection_cpy_tflite.py",
        "gguf_language_model": "language_model_gguf.py",
    }

    # 1. Try explicit entrypoint
    if entrypoint:
        candidate = SCRIPT_DIR / entrypoint
        if candidate.exists():
            return candidate
        # If entrypoint fails, we log and fall through to mapping
        print(f"Warning: Entrypoint '{entrypoint}' not found, falling back to runner mapping")

    # 2. Try Runner mapping
    if runner in runner_to_script_path:
        candidate = SCRIPT_DIR / runner_to_script_path[runner]
        if candidate.exists():
            return candidate

    # 3. Final error handling
    error_msg = "Could not find a valid inference script."
    if entrypoint:
        error_msg += f" Entrypoint '{entrypoint}' does not exist."
    if runner:
        error_msg += f" Runner '{runner}' mapping failed or file is missing."

    raise FileNotFoundError(error_msg)


def find_llm_process_by_model(model_path_str: str) -> int | None:
    """Find the PID of a running LLM inference process by model path."""
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            cmdline_str = " ".join(cmdline)
            # Check if this is the LLM script with our model
            if "language_model_gguf.py" in cmdline_str and model_path_str in cmdline_str:
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return None


def run_inference_script(script_path: Path, model_path: Path, device_id: str, api_key: str, extra_args=None):
    """Execute the selected inference script with the provided arguments."""
    command = [
        sys.executable,
        str(script_path),
        "--model",
        str(model_path),
        "--device-id",
        device_id,
        "--api-key",
        api_key,
    ]
    if extra_args:
        command.extend(extra_args)

    print(f"Executing: {' '.join(command)}")

    try:
        # For interactive scripts like the LLM, we try to open a new terminal window
        if "language_model_gguf.py" in str(script_path):
            global llm_detached_mode, llm_model_path
            llm_detached_mode = True
            llm_model_path = str(model_path)

            if os.name == "nt":
                # Windows: Use native console creation flag
                return subprocess.Popen(
                    command,
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                    text=True,
                )
            elif sys.platform == "darwin":
                # macOS: Use AppleScript to open Terminal.app
                import shlex

                shell_command = " ".join(shlex.quote(c) for c in command)
                full_cmd = f"cd {shlex.quote(os.getcwd())} && {shell_command}"
                applescript_cmd = full_cmd.replace('"', '\\"')
                applescript = (
                    'tell application "Terminal" to activate\ntell application "Terminal" to do script '
                    f'"{applescript_cmd}"'
                )
                return subprocess.Popen(["osascript", "-e", applescript])
            else:
                # Linux: Try common terminal emulators
                for term in [
                    "x-terminal-emulator",
                    "gnome-terminal",
                    "konsole",
                    "xterm",
                ]:
                    if subprocess.run(["which", term], capture_output=True, text=True).returncode == 0:
                        if term in ["gnome-terminal", "konsole"]:
                            return subprocess.Popen([term, "--", *command])

                        return subprocess.Popen([term, "-e", *command])

        # Default behavior: run in the current terminal window
        process = subprocess.Popen(command, stdout=None, stderr=None, text=True)
        return process
    except Exception as e:
        print(f"Error starting inference script: {e}", file=sys.stderr)
        return None


def main():
    """Main entry point for the model inference dispatcher."""
    parser = argparse.ArgumentParser(description="ML Edge Platform - Model Inference Wrapper")
    parser.add_argument("--model", required=True, help="Path to the model file")
    parser.add_argument("--device-id", required=True, help="Device ID")
    parser.add_argument("--api-key", required=True, help="API key for authentication")
    parser.add_argument(
        "--model-name",
        required=False,
        help="Original model filename on server (used for selecting the proper runner)",
    )
    parser.add_argument("--model-id", required=False, help="Model ID (informational; not required)")
    parser.add_argument(
        "--config",
        required=False,
        help="Path to v2 runtime config JSON to drive runner and flags",
    )

    args = parser.parse_args()

    model_path = Path(args.model)
    if not model_path.exists():
        print(f"Error: Model file not found at {model_path}", file=sys.stderr)
        sys.exit(1)

    extra_child_args = []
    script_path = None
    config_path = None

    # Determine config file to use
    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"Error: Config file not found at {config_path}", file=sys.stderr)
            sys.exit(1)
    elif args.model_id:
        found_config = find_runtime_config_file(args.model_id)
        if found_config:
            config_path = found_config
            print(f"Found runtime config: {config_path}")
        else:
            print(f"No runtime config found for model_id '{args.model_id}', using heuristics")

    # If a config is available, use it to choose runner and flags
    if config_path:
        cfg = load_json_config(config_path)
        if cfg is None:
            print(f"Error reading config file {config_path}", file=sys.stderr)
            sys.exit(1)

        print(f"Loaded runtime config from {config_path}")

        try:
            script_path = determine_inference_script_by_config(cfg)
            print(f"Selected script from config: {script_path.name}")
        except ValueError as e:
            print(f"Error determining script from config: {e}", file=sys.stderr)
            name_for_heuristics = args.model_name or model_path.name
            script_path = determine_inference_script_by_name(name_for_heuristics)
            print(f"Falling back to heuristic-based script: {script_path.name}")

        inputs = cfg.get("inputs") or []
        parameters = ((cfg.get("process") or {}).get("parameters")) or {}

        if isinstance(parameters, dict):
            if "confidence_threshold" in parameters:
                extra_child_args.extend(["--confidence-threshold", str(parameters["confidence_threshold"])])
            if "min_event_duration_sec" in parameters:
                extra_child_args.extend(["--min-event-duration", str(parameters["min_event_duration_sec"])])
            if "min_event_interval_sec" in parameters:
                extra_child_args.extend(["--min-event-interval", str(parameters["min_event_interval_sec"])])

            llm_params = {
                "n_ctx": "--n-ctx",
                "n_gpu_layers": "--n-gpu-layers",
                "temperature": "--temperature",
                "max_tokens": "--max-tokens",
                "top_p": "--top-p",
                "top_k": "--top-k",
                "repeat_penalty": "--repeat-penalty",
                "system_prompt": "--system-prompt",
            }
            for key, flag in llm_params.items():
                if key in parameters:
                    extra_child_args.extend([flag, str(parameters[key])])

            if "stream" in parameters:
                if parameters["stream"]:
                    extra_child_args.append("--stream")
                else:
                    extra_child_args.append("--no-stream")

        camera = next(
            (i for i in inputs if isinstance(i, dict) and i.get("type") == "camera"),
            None,
        )
        if camera:
            if "index" in camera:
                extra_child_args.extend(["--camera-index", str(camera["index"])])
            res = camera.get("resolution")
            if isinstance(res, (list, tuple)) and len(res) == 2:
                extra_child_args.extend(["--width", str(res[0]), "--height", str(res[1])])
            if "fps" in camera:
                extra_child_args.extend(["--fps", str(camera["fps"])])

        mic = next(
            (i for i in inputs if isinstance(i, dict) and i.get("type") == "microphone"),
            None,
        )
        if mic:
            if "sample_rate" in mic:
                extra_child_args.extend(["--sample-rate", str(mic["sample_rate"])])
            if "channels" in mic:
                extra_child_args.extend(["--channels", str(mic["channels"])])
    else:
        if args.model_name:
            script_path = determine_inference_script_by_name(args.model_name)
            print(f"Selected script from model_name: {script_path.name}")
        else:
            script_path = determine_inference_script_by_name(model_path.name)
            print(f"Selected script from local filename: {script_path.name}")

    if not script_path.exists():
        print(f"Error: Inference script not found at {script_path}", file=sys.stderr)
        sys.exit(1)

    global child_process
    child_process = run_inference_script(script_path, model_path, args.device_id, args.api_key, extra_child_args)
    if not child_process:
        print("Failed to start inference script", file=sys.stderr)
        sys.exit(1)

    print(f"Inference process started with PID {child_process.pid}")

    def _handle_term(signum, frame):
        try:
            if llm_detached_mode and llm_model_path:
                llm_pid = find_llm_process_by_model(llm_model_path)
                if llm_pid:
                    print(f"Received signal {signum}, terminating detached LLM process PID {llm_pid}...")
                    try:
                        llm_proc = psutil.Process(llm_pid)
                        llm_proc.terminate()
                        llm_proc.wait(timeout=10)
                    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                        try:
                            llm_proc.kill()
                        except psutil.NoSuchProcess:
                            pass

            if child_process and child_process.poll() is None:
                print(f"Received signal {signum}, terminating child PID {child_process.pid}...")
                child_process.terminate()
                try:
                    child_process.wait(timeout=10)
                except Exception:
                    child_process.kill()
        finally:
            sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    try:
        if llm_detached_mode:
            if os.name == "nt":
                print(f"LLM launched in new console (Windows). Using psutil to track PID {child_process.pid}...")
                try:
                    llm_proc = psutil.Process(child_process.pid)
                    llm_proc.wait()
                except psutil.NoSuchProcess:
                    print("LLM process already exited.")
            else:
                print("LLM launched in detached terminal. Polling for actual process...")
                time.sleep(3)
                llm_pid = find_llm_process_by_model(llm_model_path)
                if llm_pid:
                    print(f"Found LLM process with PID {llm_pid}. Waiting...")
                    try:
                        llm_proc = psutil.Process(llm_pid)
                        llm_proc.wait()
                    except psutil.NoSuchProcess:
                        print("LLM process already exited.")
                else:
                    print("Could not find LLM process. Waiting on launcher process instead.")
                    child_process.wait()
        else:
            child_process.wait()
    except KeyboardInterrupt:
        print("\nReceived interrupt signal, terminating inference process...")
        child_process.terminate()
        try:
            child_process.wait(timeout=10)
        except Exception:
            child_process.kill()
        sys.exit(0)


if __name__ == "__main__":
    main()
