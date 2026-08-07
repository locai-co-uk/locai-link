# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""SystemMonitor source: CPU, RAM, storage, temperature metrics via psutil."""

import logging
import platform
import shutil
import subprocess
import time

import psutil
from typing_extensions import override

from link.components.registry import ComponentRegistry, Source

logger = logging.getLogger(__name__)


@ComponentRegistry.register("system_monitor")
class SystemMonitor(Source):
    """Collects system metrics as a pipeline source."""

    # Registry of available keys for validation/reference
    AVAILABLE_METRICS = {"cpu_usage", "ram_usage", "storage_available_gb", "temperature_celsius"}

    # Sensor names to probe (first hit wins) when reading CPU temperature.
    _TEMP_SENSOR_KEYS = ("cpu_thermal", "coretemp", "k10temp", "soc_thermal")

    def __init__(self, interval: float = 1.0, metrics: list[str] | None = None):
        """Initialises the SystemMonitor.

        metrics: subset to collect (from AVAILABLE_METRICS); None means all.
        """
        self.interval = interval
        self.last_poll = 0.0

        if metrics:
            self.enabled_metrics = set(metrics)
            unknown = self.enabled_metrics - self.AVAILABLE_METRICS
            if unknown:
                logger.warning(f"SystemMonitor: Unknown metrics requested: {unknown}")
        else:
            self.enabled_metrics = self.AVAILABLE_METRICS

        if "cpu_usage" in self.enabled_metrics:
            psutil.cpu_percent(interval=None)

    @override
    def __call__(self) -> dict[str, float] | None:
        """Collects metrics, or None if the interval hasn't passed (non-blocking)."""
        now = time.time()
        if now - self.last_poll < self.interval:
            return None

        self.last_poll = now
        data = {"timestamp": now}

        if "cpu_usage" in self.enabled_metrics:
            data["cpu_usage"] = psutil.cpu_percent(interval=None)

        if "ram_usage" in self.enabled_metrics:
            data["ram_usage"] = psutil.virtual_memory().percent

        if "storage_available_gb" in self.enabled_metrics:
            try:
                free_bytes = psutil.disk_usage("/").free
                data["storage_available_gb"] = round(free_bytes / (1024**3), 2)
            except Exception:
                data["storage_available_gb"] = -1.0

        if "temperature_celsius" in self.enabled_metrics:
            data["temperature_celsius"] = self._read_temp()

        return data

    def _read_temp(self) -> float:
        """Reads CPU temperature in degrees Celsius across platforms.

        psutil.sensors_temperatures() exists only on Linux/FreeBSD; macOS and Windows
        use their own paths. All branches fall back to 0.0 rather than raise, since
        telemetry must never crash the agent.
        """
        try:
            system = platform.system()

            if system == "Linux":
                temps = psutil.sensors_temperatures() or {}  # type: ignore[attr-defined]
                return next(
                    (temps[k][0].current for k in self._TEMP_SENSOR_KEYS if temps.get(k)),
                    0.0,
                )

            if system == "Darwin":
                if shutil.which("osx-cpu-temp"):
                    out = subprocess.run(["osx-cpu-temp"], capture_output=True, text=True, timeout=2).stdout.strip()
                    return float(out.split()[0]) if out else 0.0
                return 0.0

            if system == "Windows":
                return self._read_temp_windows()
        except Exception as e:
            logger.debug(f"temperature read failed: {e}")
        return 0.0

    def _read_temp_windows(self) -> float:
        """Windows temperature via PowerShell, two-tiered, all paths falling back to 0.0:

        1. Win32_PerfFormattedData_Counters_ThermalZoneInformation (root/cimv2):
           perf counter, no admin required. Some laptops return only the Microsoft
           dummy 30.15°C (kelvin x 10 = 3030), which we filter out.
        2. MSAcpi_ThermalZoneTemperature (root/wmi): ACPI thermal zone, admin only.
           Fallback when the perf counter is empty/dummy and the process is elevated
           (e.g. running as a Windows service / Local System).
        """
        # Tier 1: non-admin perf counter
        try:
            cmd = [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance -Namespace root/cimv2 "
                "-ClassName Win32_PerfFormattedData_Counters_ThermalZoneInformation "
                "-ErrorAction SilentlyContinue | "
                "ForEach-Object { if ($_.HighPrecisionTemperature) "
                "{ $_.HighPrecisionTemperature } else { $_.Temperature } } | "
                "Select-Object -First 1",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            raw = res.stdout.strip()
            if raw:
                k_x10 = float(raw)
                # 3030 = 30.15°C, Microsoft's "no real sensor" placeholder.
                # 0 / negative also obvious junk.
                if k_x10 > 0 and k_x10 != 3030:
                    return round((k_x10 / 10.0) - 273.15, 1)
        except Exception as e:
            logger.debug(f"perf-counter temperature read failed: {e}")

        # Tier 2: ACPI (admin-only) fallback
        try:
            cmd = [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance -Namespace root/wmi "
                "-ClassName MSAcpi_ThermalZoneTemperature "
                "-ErrorAction SilentlyContinue | "
                "Select-Object -First 1 -ExpandProperty CurrentTemperature)",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            raw = res.stdout.strip()
            if raw:
                return round((float(raw) / 10.0) - 273.15, 1)
        except Exception as e:
            logger.debug(f"ACPI temperature read failed: {e}")

        return 0.0
