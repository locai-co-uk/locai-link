# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Cross-platform OS service manager (systemd, launchd, Windows Service)."""

import getpass
import logging
import platform
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# Default reverse-DNS prefix used for service labels. The new
# installer flow overrides this to "uk.co.locai.link" to match the
# bundle identifier on the .pkg / .app artefacts.
DEFAULT_LABEL_PREFIX = "io.locai"

# Service scope. "user" lands the unit file under the user's home
# directory (the historical default); "system" lands it under the
# OS-level system directory (the .pkg installer's "install for all
# users" path). Only honoured by MacOSBackend today — Linux and
# Windows backends keep their historical behaviour.
ServiceScope = Literal["user", "system"]


class ServiceBackend(ABC):
    """Abstract Base Class for OS-specific service operations."""

    def __init__(
        self,
        service_name,
        command,
        description,
        working_dir,
        env_vars,
        scope: ServiceScope = "user",
        label_prefix: str = DEFAULT_LABEL_PREFIX,
    ):
        """Initialises the service backend."""
        self.service_name = service_name
        self.command = command
        self.description = description
        self.working_dir = Path(working_dir) if working_dir else Path.cwd()
        self.env_vars = env_vars or {}
        self.home = Path.home()
        self.scope = scope
        self.label_prefix = label_prefix

        # Standardise logs across platforms
        self.log_dir = self.working_dir / "logs"
        self.log_file = self.log_dir / f"{self.service_name}.log"

    @property
    def label(self) -> str:
        """Full reverse-DNS label, e.g. ``uk.co.locai.link.agent``."""
        return f"{self.label_prefix}.{self.service_name}"

    def prepare_logs(self):
        """Prepares the log directory."""
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def is_installed(self) -> bool:
        """Checks if the service is installed."""
        pass

    @abstractmethod
    def is_running(self) -> bool:
        """Checks if the service is running."""
        pass

    @abstractmethod
    def install(self, start_now: bool):
        """Installs the service."""
        pass

    @abstractmethod
    def uninstall(self):
        """Uninstalls the service."""
        pass

    @abstractmethod
    def start(self):
        """Starts the service."""
        pass

    @abstractmethod
    def stop(self):
        """Stops the service."""
        pass


class LinuxBackend(ServiceBackend):
    """Systemd (User Mode) Implementation."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.unit_path = self.home / ".config/systemd/user" / f"{self.service_name}.service"

    def is_installed(self) -> bool:
        return self.unit_path.exists()

    def is_running(self) -> bool:
        return _run_quiet(["systemctl", "--user", "is-active", self.service_name])

    def install(self, start_now: bool):
        self.prepare_logs()
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)

        env_lines = "\n".join([f"Environment={k}={v}" for k, v in self.env_vars.items()])
        content = f"""[Unit]
Description={self.description}
After=default.target

[Service]
ExecStart={self.command}
WorkingDirectory={self.working_dir}
Restart=always
{env_lines}
StandardOutput=append:{self.log_file}
StandardError=append:{self.log_file}

[Install]
WantedBy=default.target
"""
        with open(self.unit_path, "w") as f:
            f.write(content)

        _run_cmd("systemctl --user daemon-reload")
        _run_cmd(f"systemctl --user enable {self.service_name}")

        # Ensure user services run even when user isn't logged in
        # FIX: Use getpass.getuser() instead of os.getlogin()
        _run_cmd(f"loginctl enable-linger {getpass.getuser()}", ignore_errors=True)

        if start_now:
            self.start()

    def start(self):
        _run_cmd(f"systemctl --user start {self.service_name}")

    def stop(self):
        _run_cmd(f"systemctl --user stop {self.service_name}", ignore_errors=True)

    def uninstall(self):
        if self.is_installed():
            self.stop()
            _run_cmd(f"systemctl --user disable {self.service_name}", ignore_errors=True)
            self.unit_path.unlink()
            _run_cmd("systemctl --user daemon-reload")
            _run_cmd(f"systemctl --user reset-failed {self.service_name}", ignore_errors=True)
            logger.info(f"Service {self.service_name} uninstalled.")


class MacOSBackend(ServiceBackend):
    """LaunchAgents Implementation.

    Supports two scopes:
        * ``user``   — ``~/Library/LaunchAgents/<label>.plist``. Runs
          when this user logs in. Historical default.
        * ``system`` — ``/Library/LaunchAgents/<label>.plist``. Runs
          when *any* user logs in. The .pkg installer's "Install for
          all users of this Mac" path lands here.

    System-scope writes require write access to ``/Library/`` (root or
    sudo). The .pkg postinstall script runs with the right privileges;
    the developer one-liner flow does not, which is why ``user`` stays
    the default.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        launchagents_root = (
            Path("/Library/LaunchAgents") if self.scope == "system" else self.home / "Library" / "LaunchAgents"
        )
        self.plist_path = launchagents_root / f"{self.label}.plist"

    def is_installed(self) -> bool:
        return self.plist_path.exists()

    def is_running(self) -> bool:
        # launchctl list returns 0 only when the label is currently
        # loaded. Anchor the grep with a space so e.g. "agent" doesn't
        # falsely match "agent.menubar" loaded for a different service.
        res = subprocess.run(
            f"launchctl list | grep ' {self.label}$'", shell=True, capture_output=True
        )
        return res.returncode == 0

    def install(self, start_now: bool):
        self.prepare_logs()
        parts = self.command.split()
        exe = parts[0]
        args_xml = "\n".join([f"<string>{a}</string>" for a in parts[1:]])

        # Format env vars for plist
        env_xml = ""
        if self.env_vars:
            env_xml = (
                "<key>EnvironmentVariables</key><dict>"
                + "".join([f"<key>{k}</key><string>{v}</string>" for k, v in self.env_vars.items()])
                + "</dict>"
            )

        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key> <string>{self.label}</string>
    <key>ProgramArguments</key> <array><string>{exe}</string>{args_xml}</array>
    <key>WorkingDirectory</key> <string>{self.working_dir}</string>
    <key>RunAtLoad</key> <{"true" if start_now else "false"}/>
    <key>KeepAlive</key> <true/>
    {env_xml}
    <key>StandardOutPath</key> <string>{self.log_file}</string>
    <key>StandardErrorPath</key> <string>{self.log_file}</string>
</dict>
</plist>"""

        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.plist_path, "w") as f:
            f.write(plist)

        if start_now:
            self.start()

    def start(self):
        _run_cmd(f"launchctl load {self.plist_path}")

    def stop(self):
        _run_cmd(f"launchctl unload {self.plist_path}", ignore_errors=True)

    def uninstall(self):
        if self.is_installed():
            self.stop()
            self.plist_path.unlink()
            logger.info(f"Service {self.service_name} uninstalled.")


class WindowsBackend(ServiceBackend):
    """Windows Service Implementation (Requires Admin)."""

    def is_installed(self) -> bool:
        return _run_quiet(["sc", "query", self.service_name])

    def is_running(self) -> bool:
        # 'sc query' returns 0 if service exists, but we need to check STATE
        res = subprocess.run(["sc", "query", self.service_name], capture_output=True, text=True)
        return "RUNNING" in res.stdout

    def install(self, start_now: bool):
        self.prepare_logs()
        if not _is_admin():
            logger.warning("Admin privileges required to install Windows services.")
            return

        # FIX: Escape inner quotes with backslashes (\") so sc.exe parses them correctly
        # The result looks like: binPath= "cmd /c \"python.exe ... >> log 2>&1\""
        inner_cmd = f"{self.command} >> {self.log_file} 2>&1"
        bin_path = f'cmd /c \\"{inner_cmd}\\"'

        cmd = f'sc create {self.service_name} binPath= "{bin_path}" start= auto displayname= "{self.description}"'
        _run_cmd(cmd)

        if self.env_vars:
            self._apply_service_environment()

        if start_now:
            self.start()

    def _apply_service_environment(self):
        """Write per-service environment variables to the registry.

        `sc create` cannot set environment variables; the SCM reads them from
        the REG_MULTI_SZ value `Environment` under the service's registry key
        (HKLM\\SYSTEM\\CurrentControlSet\\Services\\<name>). `sc delete`
        removes the whole key, so uninstall needs no extra cleanup.
        """
        import winreg

        entries = [f"{k}={v}" for k, v in self.env_vars.items()]
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                rf"SYSTEM\CurrentControlSet\Services\{self.service_name}",
                0,
                winreg.KEY_SET_VALUE,
            )
            try:
                winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, entries)
            finally:
                winreg.CloseKey(key)
        except OSError as e:
            logger.warning(f"Could not set service environment for {self.service_name}: {e}")

    def start(self):
        if _is_admin():
            _run_cmd(f"sc start {self.service_name}")

    def stop(self):
        if _is_admin():
            _run_cmd(f"sc stop {self.service_name}", ignore_errors=True)

    def uninstall(self):
        if _is_admin():
            self.stop()
            _run_cmd(f"sc delete {self.service_name}")
            logger.info(f"Service {self.service_name} uninstalled.")


def _run_cmd(cmd: str | list[str], ignore_errors: bool = False):
    """Executes a shell command.

    Args:
        cmd (str | list[str]): The command to run.
        ignore_errors (bool): If True, suppresses CalledProcessError.
    """
    try:
        subprocess.run(cmd, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        if not ignore_errors:
            logger.warning(f"Command failed: {cmd}")


def _run_quiet(cmd_list: list[str]) -> bool:
    """Returns True if command exit code is 0.

    Args:
        cmd_list (list[str]): The command and args.

    Returns:
        bool: True if exit code is 0, else False.
    """
    try:
        subprocess.run(cmd_list, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _is_admin() -> bool:
    """Checks for Windows Admin privileges.

    Returns:
        bool: True if admin, False otherwise.
    """
    try:
        import ctypes

        if hasattr(ctypes, "windll"):
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        return False
    except Exception:
        return False


def ServiceManager(
    service_name: str,
    command: str | None = None,
    description: str = "Loc.ai Service",
    working_dir: Path | str | None = None,
    env_vars: dict[str, str] | None = None,
    scope: ServiceScope = "user",
    label_prefix: str = DEFAULT_LABEL_PREFIX,
) -> ServiceBackend:
    """Factory function that returns the correct OS backend.

    Args:
        service_name (str): Name of the service.
        command (str | None): The command to execute.
        description (str): Description of the service.
        working_dir (Path | str | None): Working directory for the service.
        env_vars (dict[str, str] | None): Environment variables to set.
        scope: "user" (default, per-user) or "system" (.pkg "install for
            all users" path — system-wide unit file). Only honoured by
            MacOSBackend today.
        label_prefix: Reverse-DNS prefix for the service label. The new
            installer flow passes "uk.co.locai.link" to align with the
            bundle identifier on .pkg / .app artefacts. Default is
            "io.locai" to preserve historical behaviour for the
            developer one-liner flow.

    Returns:
        ServiceBackend: An instance of LinuxBackend, MacOSBackend, or WindowsBackend.

    Raises:
        NotImplementedError: If the OS is not supported.
    """
    system = platform.system().lower()
    kwargs = {"scope": scope, "label_prefix": label_prefix}

    if system == "linux":
        return LinuxBackend(service_name, command, description, working_dir, env_vars, **kwargs)
    elif system == "darwin":
        return MacOSBackend(service_name, command, description, working_dir, env_vars, **kwargs)
    elif system == "windows":
        return WindowsBackend(service_name, command, description, working_dir, env_vars, **kwargs)
    else:
        raise NotImplementedError(f"OS '{system}' is not supported.")


def install_all(services: list[ServiceBackend], start_now: bool) -> None:
    """Install several services in lockstep.

    Used by the Setup Assistant's Finish step to register the agent and
    menu-bar LaunchAgents together — both come up under the same
    "Run at login" toggle. If any install raises, every service
    already installed in this batch is rolled back so the caller doesn't
    end up with half a system registered.

    Args:
        services: ServiceBackend instances to register. Each is
            constructed by the caller with its own service_name, command,
            and label_prefix — this function just sequences the install
            calls.
        start_now: Passed straight through to each backend's
            ``install``. RunAtLoad on every plist tracks this flag.
    """
    installed: list[ServiceBackend] = []
    try:
        for svc in services:
            svc.install(start_now=start_now)
            installed.append(svc)
    except Exception:
        for svc in installed:
            try:
                svc.uninstall()
            except Exception:
                # Best-effort rollback; surface the original install
                # failure rather than the rollback exception.
                logger.exception("Rollback of %s failed", svc.label if hasattr(svc, "label") else svc.service_name)
        raise
