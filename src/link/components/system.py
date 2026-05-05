# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""SystemMonitor source — CPU, RAM, storage, temperature metrics via psutil."""

import logging
import platform
import shutil
import subprocess
import time

import psutil

from link.components.registry import ComponentRegistry, Source

logger = logging.getLogger(__name__)


@ComponentRegistry.register("system_monitor")
class SystemMonitor(Source):
    """
    Collects system metrics.
    Acts as a Pipeline Source.
    """

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

        # Prime the CPU counter so the first real call has a delta to compare against.
        # psutil.cpu_percent(interval=None) returns 0.0 on the very first call.
        if "cpu_usage" in self.enabled_metrics:
            psutil.cpu_percent(interval=None)

    def __call__(self) -> dict[str, float] | None:
        """Collects metrics (Non-Blocking).

        Returns:
            dict | None: The collected metrics or None if interval hasn't passed.
        """
        # 1. Rate Limiting
        # Instead of sleeping, we return None. This returns control to the Pipeline
        # loop immediately, allowing it to check for 'stop' signals.
        now = time.time()
        if now - self.last_poll < self.interval:
            return None

        self.last_poll = now
        data = {"timestamp": now}

        # 2. CPU
        # interval=None is non-blocking. It calculates usage since the *last call*.
        if "cpu_usage" in self.enabled_metrics:
            data["cpu_usage"] = psutil.cpu_percent(interval=None)

        # 3. RAM Usage
        if "ram_usage" in self.enabled_metrics:
            data["ram_usage"] = psutil.virtual_memory().percent

        # 4. Storage
        if "storage_available_gb" in self.enabled_metrics:
            try:
                free_bytes = psutil.disk_usage("/").free
                data["storage_available_gb"] = round(free_bytes / (1024**3), 2)
            except Exception:
                data["storage_available_gb"] = -1.0

        # 5. Temperature
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
                # Optional 3rd-party brew binary. We do NOT auto-install: brew
                # may not be present, requires sudo for some setups, and silent
                # install during agent setup would be intrusive. Document as
                # opt-in (`brew install osx-cpu-temp`); falls back to 0.0
                # silently when the binary isn't on PATH.
                if shutil.which("osx-cpu-temp"):
                    out = subprocess.run(["osx-cpu-temp"], capture_output=True, text=True, timeout=2).stdout.strip()
                    return float(out.split()[0]) if out else 0.0
                return 0.0

            if system == "Windows":
                try:
                    import wmi  # type: ignore[import-not-found]
                except ImportError:
                    return 0.0
                t = wmi.WMI(namespace="root\\wmi").MSAcpi_ThermalZoneTemperature()
                if not t:
                    return 0.0
                return round((t[0].CurrentTemperature / 10.0) - 273.15, 1)
        except Exception as e:
            logger.debug(f"temperature read failed: {e}")
        return 0.0
