# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import json
import re
from datetime import datetime
from pathlib import Path

import psutil
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Button, Footer, Header, Label, Log, Static, TabbedContent, TabPane

from link.infra.service import ServiceManager

try:
    import zenoh
except ImportError:
    zenoh = None

# --- ANSI STRIPPER REGEX ---
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
# --- JSON EXTRACTOR REGEX ---
JSON_PATTERN = re.compile(r"(\{.*\})")


class ServiceControl(Static):
    is_active = reactive(False)

    def __init__(self, service_name, display_name, dev_pattern=None, **kwargs):
        """Initialises the ServiceControl widget.

        Args:
            service_name: Name of the service.
            display_name: Display name for the widget.
            dev_pattern: Pattern to look for in process list (dev mode).
            kwargs: Additional arguments.
        """
        super().__init__(**kwargs)
        self.service_name = service_name
        self.display_name = display_name
        self.dev_pattern = dev_pattern
        self.manager = ServiceManager(service_name)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="control-row"):
            yield Label(f"⚪ {self.display_name}", id="status_lbl", classes="status-text")
            yield Button("INIT", id="toggle_btn", variant="primary", classes="compact-btn")

    def on_mount(self):
        self.check_status()
        self.set_interval(1.0, self.check_status)

    def check_status(self):
        try:
            # 1. Check Service Manager (Systemd)
            running = self.manager.is_running()

            # 2. Check Process List (Dev Mode)
            if not running and self.dev_pattern:
                for proc in psutil.process_iter(["cmdline"]):
                    try:
                        # Avoid matching ourselves (the TUI process)
                        if proc.pid == psutil.Process().pid:
                            continue

                        cmd = " ".join(proc.info["cmdline"] or [])
                        if self.dev_pattern in cmd:
                            running = True
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

            self.is_active = running
        except Exception:
            self.is_active = False

    def watch_is_active(self, active):
        try:
            lbl = self.query_one("#status_lbl", Label)
            btn = self.query_one("#toggle_btn", Button)
            if active:
                lbl.update(f"🟢 {self.display_name}")
                btn.label = "STOP"
                btn.variant = "error"
            else:
                lbl.update(f"🔴 {self.display_name}")
                btn.label = "START"  # Note: Start only works if configured as a Service
                btn.variant = "success"
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "toggle_btn":
            if self.is_active:
                self._do_stop()
            else:
                self._do_start()
            # Immediate status check
            self.check_status()

    def _do_start(self):
        """Attempts to start the service."""
        try:
            self.manager.start()
        except Exception:
            pass

    def _do_stop(self):
        """Stops service OR kills dev process."""
        # 1. Try Systemd Stop
        try:
            self.manager.stop()
        except Exception:
            pass

        # 2. Force Kill Dev Process
        if self.dev_pattern:
            self._kill_dev_process()

    def _kill_dev_process(self):
        """Finds and kills the process matching the dev pattern."""
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                if proc.pid == psutil.Process().pid:
                    continue

                cmd = " ".join(proc.info["cmdline"] or [])
                if self.dev_pattern in cmd:
                    proc.terminate()  # SIGTERM
                    try:
                        proc.wait(timeout=1.0)
                    except psutil.TimeoutExpired:
                        proc.kill()  # SIGKILL
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass


class SystemMetrics(Static):
    cpu = reactive(0.0)
    ram = reactive(0.0)
    disk = reactive(0.0)
    temp = reactive(0.0)

    def compose(self) -> ComposeResult:
        with Grid(classes="metrics-grid"):
            yield Label("CPU", classes="metric-label")
            yield Label("0.0%", id="cpu_usage", classes="metric-value")

            yield Label("RAM", classes="metric-label")
            yield Label("0.0%", id="ram_usage", classes="metric-value")

            yield Label("DSK", classes="metric-label")
            yield Label("0.0 GB", id="storage_available_gb", classes="metric-value")

            yield Label("TMP", classes="metric-label")
            yield Label("0°C", id="temperature_celsius", classes="metric-value")

    def watch_cpu(self, val):
        self.query_one("#cpu_usage", Label).update(f"{val:.1f}%")

    def watch_ram(self, val):
        self.query_one("#ram_usage", Label).update(f"{val:.1f}%")

    def watch_disk(self, val):
        self.query_one("#storage_available_gb", Label).update(f"{val:.1f} GB")

    def watch_temp(self, val):
        self.query_one("#temperature_celsius", Label).update(f"{val:.1f}°C")

    def reset(self):
        """Forces all metrics to zero."""
        self.cpu = 0.0
        self.ram = 0.0
        self.disk = 0.0
        self.temp = 0.0


class DashboardApp(App):
    TITLE = "Loc.ai:Link"

    CSS = """
    Screen { layout: vertical; background: transparent; }
    .top-section { height: 8; min-height: 8; layout: grid; grid-size: 2; grid-gutter: 1; margin: 0 1; }
    .box { height: 100%; border: round $accent; padding: 0 1; background: transparent; }
    .box > .border-title { color: $accent; text-style: bold; background: $surface; padding: 0 1; }
    .control-row { height: 2; align: center middle; margin: 0; }
    .status-text { width: 1fr; text-style: bold; color: $text; }
    
    .compact-btn { min-width: 6; height: 1; border: none; margin-left: 1; text-style: bold; }
    .compact-btn.-primary { background: $primary; color: $text; }
    .compact-btn.-success { background: $success; color: $text; }
    .compact-btn.-error   { background: $error; color: $text; }
    .compact-btn:hover { background: $accent; color: $surface; }
    
    .metrics-grid { layout: grid; grid-size: 4 4; grid-columns: auto 1fr auto 1fr; grid-gutter: 0 2; margin-top: 0; padding-top: 0;}
    .metric-label { color: $accent; text-style: bold; }
    .metric-value { text-align: right; color: $text; text-style: bold; }
    .router-ip { color: $text-muted; }
    
    .feed-container { height: 1fr; border: round $accent; background: transparent; margin: 0 1 1 1; padding: 0; layout: vertical;}
    TabbedContent { height: 100%; background: transparent; }
    TabPane { padding: 0; background: transparent; }
    Log { background: transparent; color: $text; padding: 0 1; height: 1fr; border: none; overflow-y: scroll; }
    """  # noqa: E501

    BINDINGS = [Binding("ctrl+q", "quit", "Quit", show=True, priority=True)]

    def __init__(self):
        super().__init__()
        self.zenoh_session = None
        self.sub = None
        self.files = {}
        self.file_status = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Container(classes="top-section"):
            with Vertical(classes="box") as v1:
                v1.border_title = "Device"
                # ID added for querying
                yield ServiceControl("locai-link", "Agent", dev_pattern="main.py run", id="ctrl_agent")
                yield SystemMetrics(id="metrics")

            with Vertical(classes="box") as v2:
                v2.border_title = "Network"
                yield ServiceControl("zenohd", "Router", id="ctrl_router")
                yield Label("Connecting...", id="ip_label", classes="router-ip")

        with Vertical(classes="feed-container"):
            with TabbedContent(initial="tab_live"):
                with TabPane("Live Feed", id="tab_live"):
                    yield Log(id="log_feed", highlight=False)
                with TabPane("Network Logs", id="tab_net"):
                    yield Log(id="log_network", highlight=False)
                with TabPane("Agent Logs", id="tab_agent"):
                    yield Log(id="log_agent", highlight=False)

        yield Footer()

    def on_mount(self):
        self.theme = "flexoki"

        if zenoh:
            try:
                conf = zenoh.Config()
                config_path = Path("configs/zenoh_client.json5")
                if config_path.exists():
                    conf = zenoh.Config.from_file(str(config_path))

                self.zenoh_session = zenoh.open(conf)

                # Robust ZID check
                info = self.zenoh_session.info
                if callable(info):
                    info = info()

                zid = getattr(info, "zid", None)
                if callable(zid):
                    zid = zid()

                self.query_one("#ip_label", Label).update(f"ZID: {zid}")
                self.sub = self.zenoh_session.declare_subscriber("locai/devices/**", self._on_zenoh_msg)

            except Exception as e:
                self.query_one("#ip_label", Label).update("Offline (No Router)")
                self.write_log("#log_feed", f"⚠️ Zenoh offline: {e}")
        else:
            self.query_one("#ip_label", Label).update("Zenoh Lib Missing")

        # 1. Log Tailing
        self.set_interval(1.0, self.tail_logs)

        # 2. Metric Reset Watchdog
        self.set_interval(1.5, self.check_agent_health)

    def check_agent_health(self):
        """Resets metrics to zero if the agent stops."""
        try:
            agent_ctl = self.query_one("#ctrl_agent", ServiceControl)
            if not agent_ctl.is_active:
                self.query_one("#metrics", SystemMetrics).reset()
        except Exception:
            pass

    def _on_zenoh_msg(self, sample):
        try:
            key = str(sample.key_expr)
            payload = sample.payload.to_string()
            self.call_from_thread(self.process_message, key, payload)
        except Exception:
            pass

    def process_message(self, key, payload):
        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            log_line = f"{timestamp} | {key} | {payload}"
            self.write_log("#log_feed", log_line)
        except Exception:
            pass

        data = None
        if payload.strip().startswith("{"):
            try:
                data = json.loads(payload)
            except Exception:
                pass

        if not data:
            match = JSON_PATTERN.search(payload)
            if match:
                try:
                    clean_json = match.group(1).replace("'", '"')
                    data = json.loads(clean_json)
                except Exception:
                    pass

        if data:
            self._update_metrics(data)

    def _update_metrics(self, data):
        """Maps internal metric keys to UI Widgets.

        Args:
            data: Metric data dictionary.
        """
        try:
            w = self.query_one("#metrics", SystemMetrics)

            # CPU
            if "cpu_usage" in data:
                w.cpu = data["cpu_usage"]
            elif "cpu_usage" in data:
                w.cpu = data["cpu_usage"]
            elif "cpu_percent" in data:
                w.cpu = data["cpu_percent"]

            # RAM
            if "ram_usage" in data:
                w.ram = data["ram_usage"]
            elif "ram_usage" in data:
                w.ram = data["ram_usage"]
            elif "memory_percent" in data:
                w.ram = data["memory_percent"]

            # DISK
            if "storage_available_gb" in data:
                w.disk = data["storage_available_gb"]
            elif "storage_available_gb" in data:
                w.disk = data["storage_available_gb"]
            elif "disk_gb" in data:
                w.disk = data["disk_gb"]

            # TEMP
            if "temperature_celsius" in data:
                w.temp = data["temperature_celsius"]
            elif "temperature_celsius" in data:
                w.temp = data["temperature_celsius"]
            elif "cpu_temp" in data:
                w.temp = data["cpu_temp"]

        except Exception:
            pass

    def tail_logs(self):
        log_map = {"zenohd.log": "#log_network", "agent.log": "#log_agent"}
        base_dir = Path.cwd() / "logs"

        if not base_dir.exists():
            return

        for filename, widget_id in log_map.items():
            filepath = base_dir / filename

            if not filepath.exists():
                if self.file_status.get(filename) != "missing":
                    self.write_log(widget_id, f"⚠️ Waiting for: {filename}")
                    self.file_status[filename] = "missing"
                continue

            if self.file_status.get(filename) == "missing":
                self.write_log(widget_id, f"✅ Found: {filename}")
                self.file_status[filename] = "found"

            if filename not in self.files:
                try:
                    f = open(filepath, "r", encoding="utf-8")
                    f.seek(0, 2)
                    self.files[filename] = f
                except Exception:
                    continue

            try:
                f = self.files[filename]
                lines = f.readlines()
                for line in lines:
                    clean = line.strip()
                    if clean:
                        clean_text = ANSI_ESCAPE.sub("", clean)
                        self.write_log(widget_id, clean_text)

                        # Process Agent Logs for Metrics
                        if filename == "agent.log":
                            self.process_message("LOG", clean_text)
            except Exception:
                pass

    def write_log(self, widget_id, message):
        try:
            self.query_one(widget_id, Log).write_line(str(message))
        except Exception:
            pass

    def on_unmount(self):
        if self.zenoh_session:
            self.zenoh_session.close()
        for f in self.files.values():
            try:
                f.close()
            except Exception:
                pass


def start_tui():
    app = DashboardApp()
    app.run()
