# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""OTA update logic — pulls latest code and refreshes dependencies/plugin binaries.

The agent itself signals an update request by setting `AgentRuntime.update_requested = True`
and shutting down. The caller (main.py run) then invokes `pull_and_update()` and
re-execs the Python process via `os.execv()` to load the new code.
"""

import logging
import shutil
import subprocess
import tomllib
from pathlib import Path

from link.config.models import AgentConfig

logger = logging.getLogger(__name__)


DEFAULT_BRANCH = "main"


def _command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def get_current_branch(repo_dir: Path) -> str | None:
    """Returns the current git branch name, or None if it cannot be determined.

    Args:
        repo_dir (Path): The path to the git repository.

    Returns:
        str | None: The current branch name.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch and branch != "HEAD" else None


def get_local_version(repo_dir: Path) -> str | None:
    """Reads the version string from pyproject.toml.

    Args:
        repo_dir (Path): The path to the project root.

    Returns:
        str | None: The version string, or None if not found.
    """
    toml_path = repo_dir / "pyproject.toml"
    if not toml_path.exists():
        return None
    for line in toml_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            parts = line.split("=", 1)
            if len(parts) == 2:
                return parts[1].strip().strip('"').strip("'")
    return None


def pull_and_update(repo_dir: Path, branch: str = DEFAULT_BRANCH) -> bool:
    """Pulls the latest code from the remote, stashing any local changes.

    Args:
        repo_dir (Path): The path to the git repository.
        branch (str): The default branch to pull from (overridden if on a dev branch).

    Returns:
        bool: True if the codebase was updated, False if already up to date.

    Raises:
        RuntimeError: If git is not available or the pull fails irrecoverably.
    """
    if not _command_exists("git"):
        raise RuntimeError("git is required for updates but was not found.")

    # Use the actual current branch rather than the default, so running
    # update on a dev branch doesn't pull main into it.
    current_branch = get_current_branch(repo_dir)
    if current_branch and current_branch != branch:
        logger.info(f"Detected branch '{current_branch}' — updating from origin/{current_branch}.")
        branch = current_branch

    # Fetch without merging so we can compare first
    subprocess.run(["git", "fetch", "origin", branch], cwd=repo_dir, check=True)

    # Count commits the local branch is behind the remote
    result = subprocess.run(
        ["git", "rev-list", "--count", f"HEAD..origin/{branch}"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    behind = int(result.stdout.strip() or "0")

    if behind == 0:
        local_ver = get_local_version(repo_dir)
        logger.info(f"Already up to date{f' (v{local_ver})' if local_ver else ''}.")
        return False

    logger.info(f"Update available: {behind} new commit(s) on {branch}.")

    # Check for local modifications that would block the pull
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    ).stdout.strip()

    stashed = False
    if dirty:
        logger.info("Local modifications detected — stashing before update...")
        stash_result = subprocess.run(
            ["git", "stash", "push", "--include-untracked", "-m", "locai-auto-stash"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if stash_result.returncode != 0:
            raise RuntimeError("Could not stash local changes. Aborting update to avoid data loss.")
        stashed = True

    # Pull
    subprocess.run(["git", "pull", "origin", branch], cwd=repo_dir, check=True)

    # Restore stash if we created one
    if stashed:
        pop_result = subprocess.run(
            ["git", "stash", "pop"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if pop_result.returncode != 0:
            logger.warning("Update succeeded but stash could not be re-applied cleanly.")
            logger.warning("Your changes are saved in git stash — run 'git stash show' to review.")
        else:
            logger.info("Local changes re-applied successfully.")

    # Re-install dependencies in case pyproject.toml changed
    logger.info("Updating dependencies...")
    subprocess.run(["uv", "pip", "install", "-e", "."], cwd=repo_dir, check=True)

    new_ver = get_local_version(repo_dir)
    logger.info(f"Update complete{f' — now at v{new_ver}' if new_ver else ''}.")
    return True


def reinstall_plugin_binaries(repo_dir: Path, config: AgentConfig) -> None:
    """Re-runs install.py only for plugins referenced by the active config.

    Each plugin declares its component type(s) via `[project.entry-points."locai.plugins"]`
    in its `pyproject.toml`. A plugin is installed only if at least one of those
    entry-point names appears as `source.type` or `sink.type` in the config's
    pipelines. Built-in component types (http_poll, http_post, command, etc.)
    have no plugin dir and are silently skipped.

    Plugins use tag-based caching internally, so re-runs for active plugins are
    cheap when versions haven't changed.

    Args:
        repo_dir: The path to the project root.
        config: The active agent config — determines which plugins to refresh.
    """
    plugins_dir = repo_dir / "plugins"
    if not plugins_dir.exists():
        logger.debug("No plugins/ directory — skipping binary refresh.")
        return

    referenced = _referenced_component_types(config)

    for plugin_dir in sorted(plugins_dir.iterdir()):
        install_script = plugin_dir / "install.py"
        if not install_script.is_file():
            continue

        declared = _plugin_entry_point_names(plugin_dir)
        if not declared & referenced:
            logger.debug(f"Skipping plugin '{plugin_dir.name}' — not used by active config.")
            continue

        logger.info(f"Refreshing binaries for plugin '{plugin_dir.name}'...")
        try:
            subprocess.run(["uv", "run", "python", str(install_script)], cwd=repo_dir, check=True)
        except subprocess.CalledProcessError as e:
            logger.warning(f"Plugin '{plugin_dir.name}' install failed (exit {e.returncode}) — continuing.")


def _referenced_component_types(config: AgentConfig) -> set[str]:
    """Collects all source/sink component types referenced by the config's pipelines."""
    types: set[str] = set()
    for pipeline in config.pipelines:
        if pipeline.source and pipeline.source.type:
            types.add(pipeline.source.type)
        if pipeline.sink and pipeline.sink.type:
            types.add(pipeline.sink.type)
    return types


def _plugin_entry_point_names(plugin_dir: Path) -> set[str]:
    """Reads the 'locai.plugins' entry-point names from a plugin's pyproject.toml."""
    pyproject = plugin_dir / "pyproject.toml"
    if not pyproject.is_file():
        return set()
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Could not parse {pyproject}: {e}")
        return set()
    entries = data.get("project", {}).get("entry-points", {}).get("locai.plugins", {})
    return set(entries.keys())
