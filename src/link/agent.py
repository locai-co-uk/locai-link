# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import psutil
import requests
from rich import print

import link.logger as logger
from link.logger import link_logger

# --- Configuration ---
# Assuming agent.py is in src/link/agent.py, root is 3 levels up
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "configs"
CONFIG_FILE = CONFIG_DIR / "agent_config.json"
BASE_URL = None

METRICS_INTERVAL_SECONDS = 30  # Interval for sending metrics
COMMAND_POLL_INTERVAL_SECONDS = 10  # Interval for polling for commands


# --- Helper Functions ---
def load_config() -> dict | None:
    """Loads the agent's configuration from a local file.

    Returns:
        dict | None: The configuration data, or None if the file does not exist.
    """
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return None


def save_config(device_id, api_key, api_url=None):
    """Saves the agent's configuration to a local file.

    Args:
        device_id (str): The ID of the device.
        api_key (str): The API key for the device.
        api_url (str, optional): The API URL to persist.
    """
    # Ensure the config directory exists
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    config_data = {"device_id": device_id, "api_key": api_key}

    # Store the API URL if provided
    if api_url:
        config_data["api_url"] = api_url

    with open(CONFIG_FILE, "w") as f:
        json.dump(config_data, f, indent=4)
    print(f"Agent configuration saved to {CONFIG_FILE}")


def activate_agent(device_id, user_token) -> bool:
    """Activates the agent with the backend to get a permanent API key.

    Args:
        device_id (str): The ID of the device.
        user_token (str): The user token for the device.

    Returns:
        bool: True if the activation was successful, False otherwise.
    """
    print(f"Attempting to activate device {device_id}...")
    print(f"Using API URL: {BASE_URL}")

    headers = {"Authorization": f"Bearer {user_token}"}
    payload = {"device_id": device_id}

    try:
        # First, verify the token is valid by checking the user info
        print("Verifying user token...")
        user_response = requests.get(f"{BASE_URL}/users/me", headers=headers)
        if user_response.status_code != 200:
            print(
                f"Error verifying user token: {user_response.status_code}",
                file=sys.stderr,
            )
            try:
                error_details = user_response.json().get("detail", "No details provided.")
                print(f"Details: {error_details}", file=sys.stderr)
            except json.JSONDecodeError:
                print(f"Raw Response: {user_response.text}", file=sys.stderr)
            return False

        # Token is valid, now activate the device
        print("Token verified, activating device...")
        response = requests.post(f"{BASE_URL}/agent/activate", json=payload, headers=headers)

        if response.status_code == 200:
            data = response.json()
            api_key = data.get("api_key")
            if not api_key:
                print(
                    "Error: Activation successful but no API key received.",
                    file=sys.stderr,
                )
                sys.exit(1)

            # Persist the URL used for activation
            save_config(device_id, api_key, BASE_URL)
            print("Device activated successfully.")
            return True

        else:
            print(f"Error activating device: {response.status_code}", file=sys.stderr)
            try:
                error_details = response.json().get("detail", "No details provided.")
                print(f"Details: {error_details}", file=sys.stderr)

                # Check if the device is already registered
                if "already active" in str(error_details).lower():
                    print(
                        "This device appears to be already activated. If you need to reactivate it"
                        + " please deactivate it first from the platform UI."
                    )
            except json.JSONDecodeError:
                print(f"Raw Response: {response.text}", file=sys.stderr)
            return False

    except requests.exceptions.RequestException as e:
        print(f"A network error occurred: {e}", file=sys.stderr)
        return False


def register_new_device_with_key(device_name, device_type, username, registration_key) -> tuple[str, str] | None:
    """Registers and activates a new device using a one-time registration key.

    Args:
        device_name (str): The name of the device.
        device_type (str): The type of the device.
        username (str): The username for the device.
        registration_key (str): The registration key for the device.

    Returns:
        tuple[str, str] | None: The device ID and API key, or None if the registration failed.
    """
    print(f"Attempting to register new device '{device_name}' of type '{device_type}' using registration key...")
    print(f"Using API URL: {BASE_URL}")

    if not device_type:
        print("⚠ Warning: No device type provided. Defaulting to 'other'.")
        device_type = "other"
    else:
        valid_types = ["other"]
        if device_type not in valid_types:
            print(f"⚠ Warning: Device type '{device_type}' is not natively supported. Defaulting to 'other'.")
            device_type = "other"

    payload = {
        "username": username,
        "registration_key": registration_key,
        "name": device_name,
        "device_type": device_type,
    }

    try:
        response = requests.post(f"{BASE_URL}/devices/register-with-key", json=payload)
        if response.status_code == 200:
            data = response.json()
            device_id = data.get("device_id")
            api_key = data.get("api_key")
            if not device_id or not api_key:
                print(
                    "Error: Registration successful but missing device_id or api_key.",
                    file=sys.stderr,
                )
                return None, None
            print(f"Device '{device_name}' registered successfully with ID: {device_id}")

            # Persist the URL used for registration
            save_config(device_id, api_key, BASE_URL)
            return device_id, api_key
        else:
            print(
                f"Error registering device with key: {response.status_code}",
                file=sys.stderr,
            )
            try:
                error_details = response.json().get("detail", "No details provided.")
                print(f"Details: {error_details}", file=sys.stderr)
            except json.JSONDecodeError:
                print(f"Raw Response: {response.text}", file=sys.stderr)
            return None, None
    except requests.exceptions.RequestException as e:
        print(
            f"A network error occurred during device registration with key: {e}",
            file=sys.stderr,
        )
        return None, None


def get_system_metrics() -> dict:
    """Collects system metrics using psutil.

    Returns:
        dict: A dictionary containing system metrics.
    """
    metrics = {
        "cpu_usage": psutil.cpu_percent(interval=1),
        "ram_usage": psutil.virtual_memory().percent,
        "storage_available_gb": psutil.disk_usage("/").free / (1024**3),  # Convert bytes to GB
    }

    # Try to get temperature, but handle case where it's not available
    try:
        temps = psutil.sensors_temperatures()
        if temps and "cpu_thermal" in temps and temps["cpu_thermal"]:
            metrics["temperature_celsius"] = temps["cpu_thermal"][0].current
        else:
            # Try alternative temperature keys based on the system
            for key in temps.keys():
                if temps[key]:
                    metrics["temperature_celsius"] = temps[key][0].current
                    break
            else:
                metrics["temperature_celsius"] = 0.0
    except (AttributeError, IndexError):
        metrics["temperature_celsius"] = 0.0

    return metrics


def send_metrics(device_id, api_key):
    """Collects and sends metrics to the backend.

    Args:
        device_id (str): The ID of the device.
        api_key (str): The API key for the device.
    """
    print("Collecting and sending metrics...")
    metrics = get_system_metrics()

    # Check for health spikes
    if metrics.get("cpu_usage", 0) > 90:
        link_logger.warn(
            f"High CPU usage detected: {metrics['cpu_usage']}%",
            category="health",
            target="cpu",
            state_after=metrics,
        )
    if metrics.get("ram_usage", 0) > 90:
        link_logger.warn(
            f"High RAM usage detected: {metrics['ram_usage']}%",
            category="health",
            target="ram",
            state_after=metrics,
        )

    headers = {"Authorization": f"Bearer {api_key}"}

    url = f"{BASE_URL}/agent/{device_id}/metrics"

    try:
        # Disable redirect following to detect 302 errors
        response = requests.post(url, json=metrics, headers=headers, allow_redirects=False, timeout=30)

        if response.status_code == 200:
            print(f"Metrics successfully sent: {response.json()}")
        elif response.status_code == 302:
            # Handle redirect (likely HTTP -> HTTPS)
            redirect_location = response.headers.get("Location", "unknown")
            error_msg = (
                f"HTTP 302 Redirect detected!\n"
                f"  Request URL: {url}\n"
                f"  Redirect to: {redirect_location}\n"
                f"  This usually means the API requires HTTPS instead of HTTP.\n"
                f"  Current BASE_URL: {BASE_URL}\n"
                f"  Try updating your API_URL to use HTTPS."
            )
            print(error_msg, file=sys.stderr)

            # Try the redirect location automatically
            if redirect_location:
                print(
                    f"Attempting to follow redirect to: {redirect_location}",
                    file=sys.stderr,
                )
                try:
                    redirect_response = requests.post(redirect_location, json=metrics, headers=headers, timeout=30)
                    if redirect_response.status_code == 200:
                        print(f"Metrics successfully sent after redirect: {redirect_response.json()}")
                    else:
                        print(
                            f"Error after redirect: {redirect_response.status_code} - {redirect_response.text}",
                            file=sys.stderr,
                        )
                except Exception as redirect_e:
                    print(f"Error following redirect: {redirect_e}", file=sys.stderr)
        else:
            error_msg = f"Error sending metrics: HTTP {response.status_code}"
            try:
                error_detail = response.json()
                error_msg += f"\nDetails: {json.dumps(error_detail, indent=2)}"
            except (json.JSONDecodeError, ValueError):
                error_msg += f"\nResponse: {response.text}"
            print(error_msg, file=sys.stderr)
            print(f"Request URL: {url}", file=sys.stderr)

    except requests.exceptions.RequestException as e:
        print(f"A network error occurred while sending metrics: {e}", file=sys.stderr)
        print(f"Request URL: {url}", file=sys.stderr)


def deploy_model(payload, api_key, config) -> tuple[str, str] | None:
    """Downloads and saves a model file and its runtime config from a given URL.

    Args:
        payload (dict): The payload containing model information.
        api_key (str): The API key for the device.
        config (dict): The configuration for the device.

    Returns:
        tuple[str, str] | None: The device ID and API key, or None if the registration failed.
    """
    model_id = payload.get("model_id")
    model_name = payload.get("model_name")
    file_extension = payload.get("file_extension")
    runtime_config = payload.get("runtime_config")

    # Validate payload
    missing_fields = []
    if not model_id:
        missing_fields.append("model_id")
    if not model_name:
        missing_fields.append("model_name")
    if not file_extension:
        missing_fields.append("file_extension")

    if missing_fields:
        return link_logger.fail(f"Payload for deploy_model must include: {', '.join(missing_fields)}")

    print(f"Deploying model: {model_name} ({model_id})")

    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        device_id = config["device_id"]

        # Create directories
        models_dir = PROJECT_ROOT / "models"
        models_dir.mkdir(exist_ok=True)
        configs_dir = PROJECT_ROOT / "configs"
        configs_dir.mkdir(exist_ok=True)

        # Download model with automatic retry on transient failures
        download_url = f"{BASE_URL}/models/{model_id}/download/{device_id}/agent"

        link_logger.progress(f"Starting download for {model_name}...", 0, "downloading")

        def download_model_file() -> requests.Response:
            """Inner function for retry wrapper.

            Returns:
                requests.Response: The response from the backend.
            """
            response = requests.get(download_url, headers=headers, stream=True, timeout=300)
            if response.status_code != 200:
                raise logger.ModelDownloadLog(
                    message=f"Failed to download model: HTTP {response.status_code}",
                    model_id=model_id,
                    model_name=model_name,
                    status_code=response.status_code,
                    context={"response": response.text[:500], "url": download_url},
                )
            return response

        # Use link_logger.wrap for retry with result handling
        result = link_logger.wrap(download_model_file, retries=3)
        if result.failed:
            return result.as_tuple()
        response = result.data

        link_logger.progress(
            f"Connection established. Downloading and saving {model_name}...",
            5,
            "downloading",
        )

        # Save the model file
        model_file_path = models_dir / model_name
        try:
            total_size_bytes = int(response.headers.get("content-length", 0))
            downloaded_bytes = 0
            last_logged_pct = 0

            with open(model_file_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded_bytes += len(chunk)

                    if total_size_bytes > 0:
                        progress_pct = int((downloaded_bytes / total_size_bytes) * 100)
                        # Log every 5%
                        if progress_pct >= last_logged_pct + 5:
                            link_logger.progress(
                                f"Downloading {model_name}... {progress_pct}%",
                                progress_pct,
                                "downloading",
                            )
                            last_logged_pct = progress_pct
        except IOError as e:
            raise logger.ModelSaveLog(
                message="Failed to save model file to disk",
                model_name=model_name,
                file_path=str(model_file_path),
                original_log=e,
            )

        link_logger.progress("Model saved. Processing config...", 80, "configuring")

        messages = [f"Model '{model_name}' deployed successfully to {model_file_path}"]

        # Save runtime config if provided
        if runtime_config:
            config_file_path = configs_dir / f"{model_id}.json"
            try:
                with open(config_file_path, "w", encoding="utf-8") as f:
                    json.dump(runtime_config, f, indent=2)
                messages.append(f"Runtime config saved to {config_file_path}")
                print(f"Runtime config for model {model_id} saved to {config_file_path}")
            except IOError as e:
                # Config save failure is not critical, log but continue
                print(f"Warning: Failed to save runtime config: {e}")
        else:
            print(f"No runtime config provided for model {model_id}")

        return link_logger.ok(" ".join(messages))

    except Exception as e:
        # Use link_logger.catch to log and return error tuple
        with link_logger.catch(reraise=False) as ctx:
            raise e
        return ctx.as_tuple()


# --- Global State for Process Management ---
running_processes = {}  # Tracks running model inference processes


def execute_command(command_obj, api_key, config) -> tuple[str, str]:
    """Executes a command received from the backend.

    Uses the Link log handling system for:
    - Shell command logs (CommandTimeoutLog, CommandExecutionLog)
    - Unknown commands (UnknownCommandLog)
    - Inference logs (InferenceStartLog, InferenceStopLog, etc.)

    Args:
        command_obj (dict): The command object received from the backend.
        api_key (str): The API key for the device.
        config (dict): The configuration for the device.

    Returns:
        tuple[str, str]: The device ID and API key, or failure tuple.
    """
    global running_processes

    command_type = command_obj.get("data", {}).get("command_type")
    payload = command_obj.get("data", {}).get("payload", {})
    command_id = command_obj.get("id")

    if command_type == "run_shell_command":
        shell_command = payload.get("command")
        if not shell_command:
            return link_logger.fail(
                "Payload for run_shell_command must include 'command' field",
                category="execution",
            )

        print(f"Executing shell command (ID:{command_id}): {shell_command}")
        try:
            result = subprocess.run(shell_command, shell=True, capture_output=True, text=True, timeout=300)
            output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"

            if result.returncode != 0:
                return link_logger.fail(
                    output,
                    category="execution",
                    action="run_shell_command",
                    target=shell_command,
                    state_after={"returncode": result.returncode},
                )

            return link_logger.ok(
                output,
                category="execution",
                action="run_shell_command",
                target=shell_command,
            )

        except subprocess.TimeoutExpired:
            return link_logger.fail(
                "Command execution timed out after 5 minutes",
                category="execution",
                action="run_shell_command",
            )
        except Exception as e:
            return link_logger.fail(
                f"Command execution failed: {e}",
                category="execution",
                action="run_shell_command",
            )

    # Deploy the model.
    elif command_type == "deploy_model":
        return deploy_model(payload, api_key, config)

    # Start the model inference.
    elif command_type == "start_model_inference":
        return start_model_inference(payload, api_key, config, running_processes)

    # Stop the model inference.
    elif command_type == "stop_model_inference":
        model_name = payload.get("model_name")
        if not model_name:
            return link_logger.fail("Payload must include 'model_name'")

        if model_name in running_processes:
            process_info = running_processes[model_name]
            process = process_info["process"]

            # Check if process is still running; if poll() is not None, it's already dead
            if process.poll() is None:
                pid = process.pid
                try:
                    # Windows: Kill the entire process tree to ensure children (inference scripts) die
                    if os.name == "nt":
                        try:
                            parent = psutil.Process(pid)
                            children = parent.children(recursive=True)
                            for child in children:
                                child.kill()
                            parent.kill()
                        except psutil.NoSuchProcess:
                            pass  # Already gone
                    else:  # macOS, Linux
                        # On Unix, kill the entire process group
                        os.killpg(process.pid, signal.SIGTERM)

                    process.wait(timeout=10)  # Wait for graceful exit
                    print(f"Inference process for '{model_name}' (PID {pid}) terminated.")
                    del running_processes[model_name]
                    return link_logger.ok(f"Inference stopped for model '{model_name}'.")

                except subprocess.TimeoutExpired:
                    process.kill()  # Force kill if terminate fails
                    del running_processes[model_name]
                    return link_logger.ok(f"Inference for '{model_name}' force-killed.")

                except Exception as e:
                    # If we failed to kill it, but it's gone now, that's fine
                    if process.poll() is not None:
                        del running_processes[model_name]
                        return link_logger.ok(f"Inference stopped for model '{model_name}'.")
                    return link_logger.fail(f"Error stopping inference for '{model_name}': {e}")
            else:
                # Process was tracked but is already dead
                del running_processes[model_name]
                return link_logger.ok(f"Inference process for '{model_name}' was already stopped.")
        else:
            return link_logger.fail(f"No active inference process found for model '{model_name}'")

    elif command_type == "update_runtime_config":
        model_id = payload.get("model_id")
        config_data = payload.get("config")
        variant_name = payload.get("variant_name", "default")

        if not model_id or not config_data:
            return link_logger.fail("Payload must include 'model_id' and 'config'")

        print(f"Updating runtime config for model {model_id} (variant: {variant_name})...")

        configs_dir = PROJECT_ROOT / "configs"
        configs_dir.mkdir(exist_ok=True)
        config_file = configs_dir / f"{model_id}.json"

        try:
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=2)

            return link_logger.ok(
                f"Runtime config for model '{model_id}' updated.",
                category="configuration",
                action="update",
                target=model_id,
            )
        except Exception as e:
            return link_logger.fail(f"Failed to save runtime config: {e}")

    elif command_type == "shutdown_agent":
        print("Shutdown command received. Initiating graceful shutdown...")
        os.kill(os.getpid(), signal.SIGINT)
        return link_logger.ok("Agent shutdown initiated.")

    else:
        return link_logger.fail(f"Unknown command type: {command_type}")


def poll_and_execute_commands(device_id, api_key, config):
    """Polls for commands, executes them, and reports status.

    Args:
        device_id (str): The device ID.
        api_key (str): The API key for the device.
        config (dict): The configuration for the device.
    """
    print("Checking for commands...")
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        # Fetch commands
        response = requests.get(f"{BASE_URL}/agent/{device_id}/commands", headers=headers)
        if response.status_code != 200:
            print(
                f"Error polling for commands: {response.status_code} - {response.text}",
                file=sys.stderr,
            )
            return

        commands_to_run = response.json()
        if not commands_to_run:
            return  # No new commands

        print(f"Received {len(commands_to_run)} new command(s).")

        for command in commands_to_run:
            command_id = command.get("id")
            status, output = execute_command(command, api_key, config)

            # Report status back
            status_payload = {"status": status, "output": output}
            status_response = requests.post(
                f"{BASE_URL}/agent/{device_id}/commands/{command_id}/status",
                json=status_payload,
                headers=headers,
            )

            if status_response.status_code == 200:
                print(f"Successfully reported status for command {command_id}.")
            else:
                print(
                    f"Error reporting status for command {command_id}: {status_response.text}",
                    file=sys.stderr,
                )

    except requests.exceptions.RequestException as e:
        print(f"A network error occurred while polling for commands: {e}", file=sys.stderr)


def metrics_loop(device_id, api_key, stop_event):
    """The loop for sending metrics.

    Args:
        device_id (str): The device ID.
        api_key (str): The API key for the device.
        stop_event (threading.Event): The event to signal when the loop should stop.
    """
    print(f"Starting metrics reporting every {METRICS_INTERVAL_SECONDS} seconds...")
    while not stop_event.is_set():
        send_metrics(device_id, api_key)
        stop_event.wait(METRICS_INTERVAL_SECONDS)


def command_loop(device_id, api_key, config, stop_event):
    """The loop for polling and executing commands.

    Args:
        device_id (str): The device ID.
        api_key (str): The API key for the device.
        config (dict): The configuration for the device.
        stop_event (threading.Event): The event to signal when the loop should stop.
    """
    print(f"Starting command polling every {COMMAND_POLL_INTERVAL_SECONDS} seconds...")
    while not stop_event.is_set():
        poll_and_execute_commands(device_id, api_key, config)
        stop_event.wait(COMMAND_POLL_INTERVAL_SECONDS)


def report_model_status(device_id, api_key, model_id, running, pid=None):
    """Reports model running status to the backend.

    Args:
        device_id (str): The device ID.
        api_key (str): The API key for the device.
        model_id (str): The model ID.
        running (bool): Whether the model is running.
        pid (int, optional): The process ID of the model. Defaults to None.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = f"{BASE_URL}/agent/{device_id}/models/{model_id}/status"
    payload = {"running": running, "pid": pid}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            print(f"Successfully reported model {model_id} status: running={running}")
        else:
            print(
                f"Error reporting model status: {response.status_code} - {response.text}",
                file=sys.stderr,
            )
    except requests.exceptions.RequestException as e:
        print(f"Network error while reporting model status: {e}", file=sys.stderr)


def monitor_inference_processes(device_id, api_key, stop_event):
    """Monitors running inference processes and reports when they exit.

    Args:
        device_id (str): The device ID.
        api_key (str): The API key for the device.
        stop_event (threading.Event): The event to signal when the loop should stop.
    """
    print("Starting inference process monitoring...")
    while not stop_event.is_set():
        # Check each running process
        for model_name in list(running_processes.keys()):
            process_info = running_processes.get(model_name)
            if not process_info:
                continue

            process = process_info["process"]
            model_id = process_info.get("model_id")

            # Check if process has exited
            if process.poll() is not None:
                print(f"Detected termination of inference process for '{model_name}' (exit code: {process.returncode})")

                # Remove from tracking
                del running_processes[model_name]

                # Report to backend if we have model_id
                if model_id:
                    report_model_status(device_id, api_key, model_id, running=False)
                    link_logger.info(
                        f"Model '{model_name}' inference stopped unexpectedly",
                        category="process",
                        action="stop",
                        target=model_id,
                        state_after={"exit_code": process.returncode},
                    )
                else:
                    print(
                        f"Warning: No model_id for '{model_name}', cannot report status to backend",
                        file=sys.stderr,
                    )

        # Check every 5 seconds
        stop_event.wait(5)


def set_device_status(device_id, api_key, status):
    """Sets the device's status to online or offline.

    Args:
        device_id (str): The device ID.
        api_key (str): The API key for the device.
        status (str): The status to set ("online" or "offline").
    """
    print(f"Setting device status to '{status}'...")
    headers = {"Authorization": f"Bearer {api_key}"}

    payload = {"status": status}

    # If going offline, also include a metrics dictionary to reset them
    if status == "offline":
        payload["metrics"] = {
            "cpu_usage": 0,
            "ram_usage": 0,
            "temperature_celsius": 0,
            "storage_available_gb": 0,
        }

    try:
        # Use the new agent-specific status endpoint
        response = requests.put(f"{BASE_URL}/agent/{device_id}/status", json=payload, headers=headers)
        if response.status_code == 200:
            print(f"Device status successfully set to '{status}'.")
        else:
            print(
                f"Error setting device status: {response.status_code} - {response.text}",
                file=sys.stderr,
            )
    except requests.exceptions.RequestException as e:
        print(
            f"A network error occurred while setting device status: {e}",
            file=sys.stderr,
        )


# --- Main Logic ---
def main():
    """Main function to run the agent."""
    global BASE_URL

    print("Agent starting...")

    # We define all possible arguments here so we can detect intent (Register vs Run)
    parser = argparse.ArgumentParser(description="LocAI Agent")

    # Setup Arguments
    device_group = parser.add_mutually_exclusive_group()
    device_group.add_argument("--device-id", help="Device ID")
    device_group.add_argument("--device-name", help="Device Name (for new registration)")

    parser.add_argument("--device-type", default="edge_device")
    parser.add_argument("--username", help="Platform username")
    parser.add_argument("--registration-key", help="One-time registration key")
    parser.add_argument("--api-key", help="Existing API Key")

    # Runtime Arguments
    parser.add_argument("--api-url", help="Override the API URL")

    # Parse arguments
    args = parser.parse_args()

    # Priority: CLI Arg > Config File > Default (None)
    config = load_config()

    if args.api_url:
        BASE_URL = args.api_url
        print(f"Using provided API URL: {BASE_URL}")
        # If we have a config, update the persisted URL immediately
        if config:
            save_config(config["device_id"], config["api_key"], BASE_URL)
    elif config and "api_url" in config:
        BASE_URL = config["api_url"]

    # Check if we have a URL before proceeding
    if not BASE_URL:
        # Note: In a fresh install, manager.py usually injects the default, so this catches rare edge cases.
        print("Error: No API URL provided or configured. Exiting.", file=sys.stderr)
        sys.exit(1)

    # We detect setup mode if specific flags are present, REGARDLESS of existing config.
    is_setup_mode = bool(args.device_name or args.registration_key or (args.device_id and args.api_key))

    if is_setup_mode:
        # A. New Device Registration
        if args.device_name:
            if not args.username or not args.registration_key:
                print("Error: --username and --registration-key required with --device-name.", file=sys.stderr)
                sys.exit(1)

            # Pass BASE_URL to ensure it gets saved in the new config
            device_id, api_key = register_new_device_with_key(
                args.device_name, args.device_type, args.username, args.registration_key
            )
            if not device_id or not api_key:
                sys.exit(1)

            print("✔ Registration complete. Configuration saved.")
            sys.exit(0)  # STOP HERE

        # B. Existing Device Activation / Re-configuration
        elif args.device_id:
            # B1. Manual Configuration (User provides ID + Key)
            if args.api_key:
                save_config(args.device_id, args.api_key, BASE_URL)
                print("✔ Configuration saved.")
                sys.exit(0)  # STOP HERE

            # B2. Activation via Key
            elif args.registration_key:
                print("Activating existing device with registration key...")
                payload = {
                    "device_id": args.device_id,
                    "registration_key": args.registration_key,
                    "device_type": args.device_type,
                }
                try:
                    response = requests.post(f"{BASE_URL}/agent/activate-with-key", json=payload)
                    if response.status_code == 200:
                        data = response.json()
                        api_key = data.get("api_key")
                        if not api_key:
                            print("Error: Activation successful but no API key received.", file=sys.stderr)
                            sys.exit(1)
                        save_config(args.device_id, api_key, BASE_URL)
                        print("✔ Activation complete. Configuration saved.")
                        sys.exit(0)  # STOP HERE
                    else:
                        print(f"Error activating device: {response.status_code} - {response.text}", file=sys.stderr)
                        sys.exit(1)
                except requests.exceptions.RequestException as e:
                    print(f"Network error during activation: {e}", file=sys.stderr)
                    sys.exit(1)
            else:
                print("Error: Provide --api-key or --registration-key with --device-id.", file=sys.stderr)
                sys.exit(1)

    # If we reached here, no setup arguments were passed. We must have a config to run.
    if not config:
        print("Agent is not configured.", file=sys.stderr)
        print("Please run 'manager.py register' or 'manager.py activate' first.", file=sys.stderr)
        sys.exit(1)

    print(f"Agent configured for device ID: {config['device_id']}")
    print(f"Using API URL: {BASE_URL}")

    device_id = config["device_id"]
    api_key = config["api_key"]

    # Initialise LogClient
    try:
        agent_version = version("locai-link")
    except PackageNotFoundError:
        agent_version = "0.1.0"

    logger.LogClient.get().configure(
        device_id=device_id,
        api_key=api_key,
        api_url=BASE_URL,
        agent_version=agent_version,
    )

    set_device_status(device_id, api_key, "online")

    stop_event = threading.Event()

    def shutdown_gracefully(signum, frame):
        print("\nShutdown signal received. Cleaning up...")
        stop_event.set()

        # Stop subprocesses
        for model_name, process_info in list(running_processes.items()):
            try:
                process_info["process"].terminate()
            except Exception:
                pass

        set_device_status(device_id, api_key, "offline")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_gracefully)
    signal.signal(signal.SIGTERM, shutdown_gracefully)

    # Start Threads
    metrics_thread = threading.Thread(target=metrics_loop, args=(device_id, api_key, stop_event), daemon=True)
    command_thread = threading.Thread(target=command_loop, args=(device_id, api_key, config, stop_event), daemon=True)
    monitoring_thread = threading.Thread(
        target=monitor_inference_processes, args=(device_id, api_key, stop_event), daemon=True
    )

    metrics_thread.start()
    command_thread.start()
    monitoring_thread.start()

    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown_gracefully(signal.SIGINT, None)


def start_model_inference(payload: dict, api_key: str, config: dict, running_processes) -> tuple[str, str]:
    """Starts the model inference for a given model.

    Uses the Link log handling system for:
    - Missing model_name (InferenceStartLog)
    - Model already running (InferenceAlreadyRunningLog)
    - Model file not found (ModelNotFoundLog)
    - Config download logs (ConfigDownloadLog via APIResponseLog)
    - Process start failures (InferenceStartLog)

    Args:
        payload: The payload containing the model name and other parameters.
        api_key: The API key for the device.
        config: The configuration for the agent.
        running_processes: The dictionary of running processes.

    Returns:
        A tuple containing the status and the output of the model inference.
        The status can be 'completed', 'failed', or 'running'.
        The output is the output of the model inference.
    """
    model_name = payload.get("model_name")
    model_id = payload.get("model_id")

    # Validate model_name
    if not model_name:
        return link_logger.fail("Payload must include 'model_name'")

    # Check if this model is already running
    if model_name in running_processes and running_processes[model_name]["process"].poll() is None:
        pid = running_processes[model_name]["process"].pid
        return link_logger.fail(f"Inference for model '{model_name}' is already running (PID {pid})")

    # Construct the full path to the model file
    model_path = PROJECT_ROOT / "models" / model_name
    if not model_path.exists():
        return link_logger.fail(f"Model file not found: {model_path}")

    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        device_id_for_compare = config["device_id"]
        local_cfg = None
        try:
            if payload.get("config_override") is not None:
                co = payload.get("config_override")
                if isinstance(co, (dict, list)):
                    local_cfg = co
                elif isinstance(co, str):
                    try:
                        local_cfg = json.loads(co)
                    except json.JSONDecodeError:
                        local_cfg = {"raw": co}
                else:
                    local_cfg = co
            else:
                cfg_dir = PROJECT_ROOT / "configs"
                if model_id:
                    cfg_file = cfg_dir / f"{model_id}.json"
                else:
                    cfg_file = cfg_dir / f"{Path(model_name).stem}.json"
                if cfg_file.exists():
                    local_cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            local_cfg = None
        if local_cfg is None:
            local_cfg = {}
        body = {
            "model_id": model_id or Path(model_name).stem,
            "local_config": local_cfg,
            "variant_name": payload.get("variant_name"),
            "ignore_paths": payload.get("ignore_paths"),
        }
        url = f"{BASE_URL}/agent/{device_id_for_compare}/config/compare"
        resp = requests.post(url, json=body, headers=headers, timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            identical = bool(result.get("are_identical"))
            if identical:
                print("[bold green]Config comparison: MATCH[/bold green]")
                link_logger.ok(
                    "Runtime config matches database",
                    category="configuration",
                    action="compare",
                    target=body["model_id"],
                    state_after=result,
                )
            else:
                diffs = result.get("differences", [])
                added = result.get("added_in_b", [])
                removed = result.get("removed_in_b", [])
                modified = result.get("modified", [])
                sample_mod = ", ".join(modified[:5])
                sample_add = ", ".join(added[:5])
                sample_rem = ", ".join(removed[:5])
                summary = (
                    f"Differences found — modified({len(modified)}): {sample_mod or 'none'}; "
                    f"added({len(added)}): {sample_add or 'none'}; "
                    f"removed({len(removed)}): {sample_rem or 'none'}"
                )
                print(f"[bold yellow]Config comparison: DIFFERENCES[/bold yellow] {summary}")
                for d in diffs[:10]:
                    path = d.get("path")
                    change = d.get("change_type")
                    va = d.get("value_a")
                    vb = d.get("value_b")
                    print(f" - [{change}] {path}: A={va} B={vb}")
                link_logger.warn(
                    f"Runtime config differs from database. {summary}",
                    category="configuration",
                    action="compare",
                    target=body["model_id"],
                    state_after=result,
                )
        else:
            try:
                err = resp.json()
            except Exception:
                err = {"detail": resp.text}
            print(f"[bold red]Config comparison failed[/bold red]: HTTP {resp.status_code}")
            link_logger.fail(
                f"Config comparison failed: HTTP {resp.status_code}",
                category="configuration",
                action="compare",
                target=body["model_id"],
                context={"response": err, "url": url},
            )
    except Exception as e:
        print(f"[bold red]Config comparison error[/bold red]: {e}")
        link_logger.warn(
            f"Config comparison error: {e}",
            category="configuration",
            action="compare",
            target=model_id or Path(model_name).stem,
        )

    # Command to run the inference wrapper script (determines image vs audio)
    inference_script_path = Path(__file__).parent / "inference" / "dispatcher.py"

    device_id = config["device_id"]
    command = [
        sys.executable,
        str(inference_script_path),
        "--model",
        str(model_path),
        "--device-id",
        device_id,
        "--api-key",
        api_key,
    ]

    # If a resolved config URL or inline override is provided, download/write it and pass to wrapper
    try:
        cfg_url = payload.get("config_url")
        cfg_override = payload.get("config_override")
        cfg_path = None
        if cfg_url or cfg_override is not None:
            configs_dir = PROJECT_ROOT / "configs"
            configs_dir.mkdir(exist_ok=True)
            model_id_for_cfg = payload.get("model_id") or Path(model_name).stem
            cfg_filename = f"{model_id_for_cfg}.json"
            cfg_path = configs_dir / cfg_filename

            if cfg_url:
                # Support absolute URLs and API-relative paths
                headers = {"Authorization": f"Bearer {api_key}"}
                if cfg_url.startswith("http://") or cfg_url.startswith("https://"):
                    full_url = cfg_url
                else:
                    full_url = f"{BASE_URL}{cfg_url}"

                # Download config with retry using link.wrap
                def download_config():
                    resp = requests.get(full_url, headers=headers, timeout=30)
                    if resp.status_code != 200:
                        raise logger.ConfigDownloadLog(
                            message=f"Failed to download config: HTTP {resp.status_code}",
                            config_url=full_url,
                            model_id=model_id,
                            context={"response": resp.text[:500]},
                        )
                    return resp

                result = link_logger.wrap(download_config, retries=3)
                if result.failed:
                    return result.as_tuple()
                cfg_path.write_bytes(result.data.content)
            else:
                # Inline override provided; accept dict or string
                try:
                    if isinstance(cfg_override, (dict, list)):
                        cfg_json_bytes = json.dumps(cfg_override).encode("utf-8")
                    elif isinstance(cfg_override, str):
                        try:
                            json.loads(cfg_override)
                            cfg_json_bytes = cfg_override.encode("utf-8")
                        except json.JSONDecodeError:
                            cfg_json_bytes = cfg_override.encode("utf-8")
                    else:
                        cfg_json_bytes = json.dumps(cfg_override).encode("utf-8")
                    cfg_path.write_bytes(cfg_json_bytes)
                except Exception as e:
                    return link_logger.fail(f"Failed to write inline config override: {e}")

            command.extend(["--config", str(cfg_path)])
        else:
            print("No explicit config provided, wrapper will attempt auto-discovery for model_id")

    except Exception as e:
        return link_logger.fail(f"Config setup failed: {e}")

    # Provide additional context to the wrapper for better selection
    if payload.get("model_name"):
        command.extend(["--model-name", payload["model_name"]])
    if payload.get("model_id"):
        command.extend(["--model-id", payload["model_id"]])

    try:
        print(f"Starting inference for model '{model_name}'...")

        # Prepare environment
        env = os.environ.copy()
        env["BASE_URL"] = BASE_URL

        if os.name == "nt":  # Windows
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                # Create new process group is good practice but optional for simple termination;
                # sticking to previous simple Popen but with pipes consumed.
            )
        else:  # macOS, Linux
            # Set up display for OpenCV on Linux
            if "DISPLAY" not in env:
                env["DISPLAY"] = ":0"
            env["OPENCV_VIDEOIO_PRIORITY_V4L2"] = "1"

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                env=env,
            )

        # Helper to stream process output
        def _stream_process_output(prefix, stream):
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    print(f"[{prefix}] {line.strip()}")
            except Exception as e:
                print(f"[{prefix}] Error reading stream: {e}")
            finally:
                stream.close()

        # Start threads to print stdout and stderr
        threading.Thread(
            target=_stream_process_output,
            args=(f"{model_name}-stdout", process.stdout),
            daemon=True,
        ).start()
        threading.Thread(
            target=_stream_process_output,
            args=(f"{model_name}-stderr", process.stderr),
            daemon=True,
        ).start()

        print(f"Inference process (PID {process.pid}) started for model '{model_name}'.")
        # Store process info with model_id for monitoring and status reporting
        running_processes[model_name] = {
            "process": process,
            "model_id": payload.get("model_id"),
        }
        return link_logger.ok(f"Inference started for model '{model_name}' with PID {process.pid}.")

    except Exception as e:
        return link_logger.fail(f"Failed to start inference subprocess: {e}")


# Start the agent
if __name__ == "__main__":
    main()
