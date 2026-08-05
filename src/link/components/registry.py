# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Component Registry for self-registration of pipeline components."""

import importlib
import importlib.metadata
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Component(Protocol):
    """The interface that all Source/Sink components must implement."""


class Source(Component):
    """A component that produces data."""

    def __call__(self) -> dict[str, Any] | None:
        """Produces data, or None when there is nothing to emit."""
        ...


class Sink(Component):
    """A component that consumes data."""

    def __call__(self, data: Any) -> bool | None:
        """Consumes data; returns True on success, False otherwise."""
        ...


class ComponentRegistry:
    """Maps type names to component classes; components self-register via @register."""

    _components: dict[str, type] = {}
    _installed_plugins: set[str] = set()

    @classmethod
    def register(cls, name: str) -> Callable[[type], type]:
        """Decorator registering a component class under a unique name.

        Usage:
            @ComponentRegistry.register("clock_tick")
            class ClockTick: ...
        """

        def decorator(component_cls: type) -> type:
            if name in cls._components:
                logger.warning(f"Component '{name}' already registered, overwriting.")
            cls._components[name] = component_cls
            return component_cls

        return decorator

    @classmethod
    def get(cls, name: str) -> type | None:
        """Get a component class by name, or None if not registered."""
        return cls._components.get(name)

    @classmethod
    def all(cls) -> dict[str, type]:
        """Return a copy of all registered components."""
        return cls._components.copy()

    @classmethod
    def install_plugin(cls, name: str) -> None:
        """Install a plugin's dependencies without instantiating it.

        Same flow as `load_plugin`'s install step, used by the `install-plugin`
        CLI command and by callers that want to pre-stage a plugin's deps and
        native binaries before deciding whether to load it.
        """
        plugin_dir = cls._find_plugin_dir(name)
        if not plugin_dir:
            raise FileNotFoundError(f"Plugin directory not found for '{name}'")
        cls._install_plugin_dependencies(name, plugin_dir)

    @classmethod
    def load_plugin(cls, name: str, args: dict[str, Any]) -> Component:
        """Dynamically installs and loads a plugin component (args initialise it)."""
        frozen = bool(getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None))

        # 1. Locate the plugin directory
        plugin_dir = cls._find_plugin_dir(name)

        # 2. Install dependencies (lazy). Skipped in a frozen bundle, where every
        # plugin is pre-installed at build time and there's no live venv to pip into.
        if plugin_dir and not frozen:
            cls._install_plugin_dependencies(name, plugin_dir)

        # 3. Load via Entry Point (Modern Standard)
        cls._refresh_entry_points()
        plugin_cls = cls._get_entry_point_class(name)

        if not plugin_cls:
            raise ValueError(
                f"Could not load plugin '{name}'. Ensure it has a pyproject.toml with 'locai.plugins' entry-points."
            )

        # 4. Instantiate
        try:
            return plugin_cls(**args)
        except Exception as e:
            raise ValueError(f"Failed to instantiate plugin '{name}': {e}")

    @staticmethod
    def _find_plugin_dir(name: str) -> Path | None:
        """Locates the plugin directory in likely locations."""
        candidates = [
            Path.cwd() / "plugins" / name,
            Path.cwd().parent / "plugins" / name,
        ]
        for p in candidates:
            if p.exists() and p.is_dir():
                return p
        return None

    @classmethod
    def _install_plugin_dependencies(cls, name: str, plugin_dir: Path):
        """Installs dependencies if not already installed."""
        if name in cls._installed_plugins:
            return

        logger.info(f"Preparing plugin: {name}...")

        # Check if 'uv' command is available
        uv_cmd = shutil.which("uv")
        if not uv_cmd:
            logger.warning("The 'uv' tool is not in PATH. Plugin installation might fail.")
            uv_cmd = "uv"

        # A. Python Dependencies (pyproject.toml / uv)
        if (plugin_dir / "pyproject.toml").exists():
            logger.info(f"Installing package from {plugin_dir.name}...")
            try:
                # This ensures we install into the CURRENT venv, even if uv is external.
                subprocess.run(
                    [uv_cmd, "pip", "install", "--python", sys.executable, "-e", str(plugin_dir)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to install python package: {e.stderr}")
                raise RuntimeError(f"Dependency installation failed for {name}")

        # B. Custom Install Script (install.py)
        # Always invoked; the script self-detects "already installed" (typically a
        # pinned-tag check) and stays quiet. Our intro is DEBUG so a no-op run is silent.
        install_script = plugin_dir / "install.py"
        if install_script.exists():
            logger.debug(f"Running custom install script for {name}...")
            try:
                subprocess.run([sys.executable, str(install_script)], cwd=plugin_dir, check=True)
            except subprocess.CalledProcessError as e:
                logger.error(f"Custom install script failed: {e}")
                raise

        # C. Legacy requirements.txt (Fallback)
        req_file = plugin_dir / "requirements.txt"
        if req_file.exists() and not (plugin_dir / "pyproject.toml").exists():
            logger.info(f"Installing legacy requirements for {name}...")
            try:
                subprocess.run(
                    [
                        uv_cmd,
                        "pip",
                        "install",
                        "--python",
                        sys.executable,
                        "-r",
                        str(req_file),
                    ],
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError:
                # Last resort fallback to pip module
                logger.warning("uv install failed, falling back to pip module...")
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
                    check=True,
                )

        cls._installed_plugins.add(name)

    @staticmethod
    def _refresh_entry_points():
        """Forces a refresh of importlib metadata."""
        importlib.metadata.packages_distributions()

    @staticmethod
    def _get_entry_point_class(plugin_name: str) -> type | None:
        """Finds the class using 'locai.plugins' entry points."""
        eps = importlib.metadata.entry_points(group="locai.plugins")
        for ep in eps:
            normalized_dist = ep.dist.name.replace("-", "_").lower() if ep.dist else ""
            if ep.name == plugin_name or normalized_dist.endswith(f"plugin_{plugin_name}"):
                logger.info(f"Loaded {plugin_name} via entry point: {ep.name}")
                return ep.load()
        return None
