# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import atexit
import json
import logging
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from colorama import Fore, Style

# --- LOCAL IMPORTS ---
try:
    from .server import ModelServer
    from .swap_manager import SwapManager, get_swap_manager
    from .install import BIN_LLAMA_DIR, LLAMA_SWAP_RELEASE, _is_swap_installed
except ImportError:
    from server import ModelServer
    from swap_manager import SwapManager, get_swap_manager
    from install import BIN_LLAMA_DIR, LLAMA_SWAP_RELEASE, _is_swap_installed


logger = logging.getLogger(__name__)


class LanguageModel:
    def __init__(
        self,
        model_path,
        mode="chat",
        n_gpu_layers=35,
        n_ctx=2048,
        new_terminal=False,
        system_prompt="You are a helpful assistant.",
        host="127.0.0.1",
        port=8100,
        alias="locai-model",
        **kwargs,
    ):
        self.mode = mode
        self.queue = queue.Queue(maxsize=10)
        self.running = True
        self.model_path = self._resolve_path(model_path)
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model not found: {self.model_path}")

        self.model_id = alias or self.model_path.stem
        self.n_gpu_layers = int(n_gpu_layers or 35)
        self.n_ctx = int(n_ctx or 2048)
        self.host = host
        self.port = int(port)

        self.server: ModelServer | None = None
        self._swap_manager: SwapManager | None = None

        if self.mode == "serve":
            if _is_swap_installed(LLAMA_SWAP_RELEASE):
                self._swap_manager = get_swap_manager(self.port, self.host, BIN_LLAMA_DIR)
                extra_args = ["--n-gpu-layers", str(self.n_gpu_layers), "--ctx-size", str(self.n_ctx)]
                self._swap_manager.add_model(
                    self.model_id, str(self.model_path), extra_args, self._build_serve_env()
                )
            else:
                logger.warning("llama-swap not installed — falling back to single-model direct serve")
                self.server = ModelServer(
                    model_path=self.model_path,
                    host=self.host,
                    port=self.port,
                    n_gpu_layers=self.n_gpu_layers,
                    n_ctx=self.n_ctx,
                    alias=alias,
                    on_telemetry=self._on_server_log,
                )
                self.server.start()
            self.thread = threading.Thread(target=self._server_heartbeat_loop, daemon=True)
            self.thread.start()
        else:
            # Chat mode: spawn a local llama-server and open an interactive loop.
            self.server = ModelServer(
                model_path=self.model_path,
                host=self.host,
                port=self.port,
                n_gpu_layers=self.n_gpu_layers,
                n_ctx=self.n_ctx,
                alias=alias,
                on_telemetry=None,
            )
            self.server.start()
            self.system_prompt = system_prompt
            self.new_terminal = new_terminal
            self.remote_conn = None

            self.parameters = {
                "temperature": float(kwargs.get("temperature", 0.7)),
                "max_tokens": int(kwargs.get("max_tokens", 2000)),
                "stream": kwargs.get("stream", True),
                "top_p": float(kwargs.get("top_p", 0.95)),
                "repeat_penalty": float(kwargs.get("repeat_penalty", 1.1)),
            }

            self.messages = [{"role": "system", "content": self.system_prompt}]
            self.thread = threading.Thread(target=self._client_interaction_loop, daemon=True)
            self.thread.start()
            logger.info("Chat initialised.")

        atexit.register(self.stop)

    def __call__(self):
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None

    def stop(self):
        self.running = False
        if self._swap_manager:
            self._swap_manager.remove_model(self.model_id)
        if self.server:
            self.server.stop()
        if getattr(self, "remote_conn", None):
            try:
                if self.remote_conn is not None:
                    self.remote_conn.close()
            except Exception:
                pass

    def wait_until_ready(self, timeout: float) -> bool:
        """Block until the serving endpoint is healthy or timeout elapses.

        Dispatches to the SwapManager health poll (swap mode) or to the
        ModelServer's own health watcher (direct mode).
        """
        if self._swap_manager:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self._swap_manager.is_healthy():
                    return True
                time.sleep(0.5)
            return False
        if self.server:
            return self.server.wait_until_ready(timeout)
        return False

    def _on_server_log(self, line):
        """Parses server logs to extract telemetry (Serve Mode Only)."""
        # Example: "eval time = 123.45 ms / 50 tokens"
        if "eval time =" in line and "prompt" not in line:
            try:
                # Parse Duration
                dur_match = re.search(r"=\s+(\d+\.\d+)\s+ms", line)
                duration_ms = float(dur_match.group(1)) if dur_match else 0.0

                # Parse Tokens
                token_match = re.search(r"/\s+(\d+)\s+tokens", line)
                tokens = int(token_match.group(1)) if token_match else 0

                now = datetime.now()
                start_time = now - timedelta(milliseconds=duration_ms)

                # Use Shared Builder
                payload = ModelServer.build_telemetry_payload(
                    model_id=self.model_id,
                    output_text="stats_only",  # Text unavailable in logs
                    start_time=start_time,
                    end_time=now,
                    duration=duration_ms / 1000.0,
                    metadata={"tokens_generated": tokens, "source": "server_log"},
                )

                if not self.queue.full():
                    self.queue.put(payload)
            except Exception:
                pass

    def _server_heartbeat_loop(self):
        while self.running:
            if self._swap_manager:
                if not self._swap_manager.is_healthy():
                    logger.warning("llama-swap health check failed", extra={"category": "health"})
            elif self.server and not self.server.running:
                logger.error("Server process died!", extra={"category": "health"})
                self.running = False
                break
            time.sleep(10)

    def _client_interaction_loop(self):
        if self.new_terminal:
            self._run_remote_ui()
        else:
            self._run_local_ui()

    def _run_local_ui(self):
        if not sys.stdin.isatty():
            return
        print(f"\n{Fore.YELLOW}--- Interactive Chat Started ---{Style.RESET_ALL}")
        while self.running:
            try:
                user_input = input(f"{Fore.GREEN}\nUser: {Style.RESET_ALL}")
                self._process_turn(user_input, output_func=print)
            except (EOFError, KeyboardInterrupt):
                self.running = False
                break

    def _run_remote_ui(self):
        ui_listener = None
        try:
            ui_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ui_listener.bind(("127.0.0.1", 0))
            port = ui_listener.getsockname()[1]
            ui_listener.listen(1)
            logger.info(f"Waiting for remote terminal on port {port}...")

            client_script = Path(__file__).parent / "client.py"
            if not client_script.exists():
                return

            self._launch_terminal(client_script, port)
            self.remote_conn, addr = ui_listener.accept()
            logger.info(f"Connected to remote terminal: {addr}")
            self._net_send(f"\n{Fore.YELLOW}--- Interactive Chat Started ---{Style.RESET_ALL}\n")

            while self.running:
                self._net_send(f"{Fore.GREEN}\nUser: {Style.RESET_ALL}")
                user_input = self._net_recv()
                if not user_input:
                    break
                self._process_turn(user_input, output_func=lambda x, end="\n", flush=False: self._net_send(x + end))

        except Exception:
            self.running = False
        finally:
            if ui_listener:
                ui_listener.close()

    def _process_turn(self, user_input, output_func):
        if user_input.lower() in ("quit", "exit"):
            self.running = False
            return

        self.messages.append({"role": "user", "content": user_input})
        output_func(f"{Fore.BLUE}Assistant: {Style.RESET_ALL}", end="")

        start_time = datetime.now()
        response_text = ""
        tokens = 0
        url = f"http://{self.host}:{self.port}/v1/chat/completions"
        # model field is required by llama-swap for routing; harmless for direct llama-server
        payload = {"model": self.model_id, "messages": self.messages, **self.parameters}

        try:
            resp = requests.post(url, json=payload, stream=self.parameters["stream"], timeout=600)
            resp.raise_for_status()

            if self.parameters["stream"]:
                for line in resp.iter_lines():
                    if line:
                        decoded = line.decode("utf-8").replace("data: ", "")
                        if decoded == "[DONE]":
                            break
                        try:
                            chunk = json.loads(decoded)
                            content = chunk["choices"][0]["delta"].get("content", "")
                            if content:
                                output_func(content, end="", flush=True)
                                response_text += content
                                tokens += 1
                        except Exception:
                            pass
            else:
                data = resp.json()
                response_text = data["choices"][0]["message"]["content"]
                output_func(response_text, end="", flush=True)
                tokens = data.get("usage", {}).get("completion_tokens", len(response_text) // 4)

        except Exception as e:
            output_func(f"\n[Error: {e}]")
            return

        output_func("")
        self.messages.append({"role": "assistant", "content": response_text})

        # --- TELEMETRY (Client Mode) ---
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        # Use Shared Builder
        telemetry_payload = ModelServer.build_telemetry_payload(
            model_id=self.model_id,
            output_text=response_text,
            start_time=start_time,
            end_time=end_time,
            duration=duration,
            metadata={
                "tokens_generated": tokens,
                "temperature": self.parameters.get("temperature"),
                "source": "client_chat",
            },
        )

        if not self.queue.full():
            self.queue.put(telemetry_payload)

    def _build_serve_env(self) -> dict[str, str]:
        """Return env vars to inject into the llama-server launched by llama-swap.

        On Linux, LD_LIBRARY_PATH must include bin-llama so the CUDA/Vulkan
        shared libraries (.so) bundled alongside llama-server are found at
        runtime.  Other platforms don't need this.
        """
        if platform.system() != "Linux":
            return {}
        bin_dir = BIN_LLAMA_DIR
        lib_paths: set[str] = {str(bin_dir)}
        for root, _, files in os.walk(bin_dir):
            for f in files:
                if "ggml" in f and f.endswith(".so"):
                    lib_paths.add(root)
        ld_path = os.pathsep.join(sorted(lib_paths))
        current = os.environ.get("LD_LIBRARY_PATH", "")
        if current:
            ld_path = f"{ld_path}{os.pathsep}{current}"
        return {"LD_LIBRARY_PATH": ld_path} if ld_path else {}

    def _resolve_path(self, path_str):
        path = Path(path_str)
        if path.exists():
            return path
        if (Path.cwd().parent / path_str).exists():
            return Path.cwd().parent / path_str
        return path

    def _net_send(self, text):
        if self.remote_conn:
            data = text.encode("utf-8")
            self.remote_conn.sendall(len(data).to_bytes(4, "big") + data)

    def _net_recv(self):
        if not self.remote_conn:
            return None
        try:
            lb = self.remote_conn.recv(4)
            if not lb:
                return None
            length = int.from_bytes(lb, "big")
            return self.remote_conn.recv(length).decode("utf-8").strip()
        except Exception:
            return None

    def _launch_terminal(self, script, port):
        sys_name = platform.system()
        script_str = str(script.resolve())
        if sys_name == "Windows":
            cmd = f'"{script_str}" --port {port}'
            subprocess.Popen(f'start "Chat" cmd /k python {cmd}', shell=True)
        elif sys_name == "Darwin":
            subprocess.Popen(["open", "-a", "Terminal", script_str, "--args", "--port", str(port)])
        elif sys_name == "Linux":
            if shutil.which("gnome-terminal"):
                subprocess.Popen(["gnome-terminal", "--", sys.executable, script_str, "--port", str(port)])
            elif shutil.which("x-terminal-emulator"):
                subprocess.Popen(["x-terminal-emulator", "-e", f"{sys.executable} {script_str} --port {port}"])
            elif shutil.which("xterm"):
                subprocess.Popen(["xterm", "-e", sys.executable, script_str, "--port", str(port)])
