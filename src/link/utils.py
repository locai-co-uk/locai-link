# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import json
import shutil
import sys
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIGS_DIR = PROJECT_ROOT / "configs"
MODELS_DIR = PROJECT_ROOT / "models"
AGENT_CONFIG_PATH = CONFIGS_DIR / "agent_config.json"


def load_json_config(path: Path) -> dict | None:
    """Safe JSON loader."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error loading config {path}: {e}", file=sys.stderr)
        return None


def command_exists(cmd: str) -> bool:
    """Checks if a command exists in the system PATH."""
    return shutil.which(cmd) is not None


def is_process_running(pid_file: Path) -> bool:
    """Checks if a process is running based on a PID file."""
    if not pid_file.exists():
        return False

    try:
        pid = int(pid_file.read_text().strip())
        if psutil and psutil.pid_exists(pid):
            return True
        else:
            # Stale PID file
            pid_file.unlink()
            return False
    except (ValueError, OSError):
        pid_file.unlink(missing_ok=True)
        return False


def stop_process_tree(pid_file: Path, log_name: str = "Process"):
    """Stops a process and its children gracefully."""
    if not pid_file.exists():
        print(f"No {log_name} PID file found.")
        return

    if not psutil:
        print("❌ Error: psutil module not found. Cannot stop process safely.")
        return

    try:
        pid = int(pid_file.read_text().strip())
        if psutil.pid_exists(pid):
            print(f"Terminating {log_name} (PID {pid})...")
            parent = psutil.Process(pid)

            children = parent.children(recursive=True)
            for child in children:
                child.terminate()

            parent.terminate()

            gone, alive = psutil.wait_procs([parent] + children, timeout=5)

            for p in alive:
                p.kill()

            print(f"{log_name} stopped.")
        else:
            print(f"{log_name} not found (already stopped).")
    except ValueError:
        print(f"Invalid {log_name} PID file.")
    except Exception as e:
        print(f"❌ Error stopping {log_name}: {e}")
    finally:
        pid_file.unlink(missing_ok=True)
