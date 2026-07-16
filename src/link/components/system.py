# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""SystemMonitor source — CPU, RAM, storage, temperature metrics via psutil."""

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

        Args:
            interval (float): Seconds to wait between collections.
            metrics (list[str] | None): Optional list of specific metrics to collect.
                If None, defaults to ALL available metrics.
                Options: ["cpu_usage", "ram_usage", "storage_available_gb", "temperature_celsius"]
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
        """Collects metrics (Non-Blocking).

        Returns:
            dict | None: The collected metrics or None if interval hasn't passed.
        """
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

        psutil.sensors_temperatures() only exists on Linux/FreeBSD; macOS and
        Windows need their own paths. All branches fall back to 0.0 on any
        failure rather than raise — telemetry should never crash the agent.
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
        """Windows temperature via PowerShell, two-tiered:

        1. `Win32_PerfFormattedData_Counters_ThermalZoneInformation` in
           `root/cimv2` — Performance Counter, **no admin required**. Works on
           any unprivileged user shell. Caveat: vendor must populate the
           counter; some laptops return only the Microsoft dummy 30.15°C
           (kelvin × 10 = 3030) which we filter out.
        2. `MSAcpi_ThermalZoneTemperature` in `root/wmi` — ACPI thermal zone,
           **requires admin**. Used as fallback when the perf counter is
           empty/dummy and the process happens to be elevated (e.g. running
           as a Windows service / Local System).

        All paths fall back to 0.0 on any failure.
        """
        # Tier 1 — non-admin perf counter
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
                # 3030 = 30.15°C — Microsoft's "no real sensor" placeholder.
                # 0 / negative also obvious junk.
                if k_x10 > 0 and k_x10 != 3030:
                    return round((k_x10 / 10.0) - 273.15, 1)
        except Exception as e:
            logger.debug(f"perf-counter temperature read failed: {e}")

        # Tier 2 — ACPI (admin-only) fallback
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
