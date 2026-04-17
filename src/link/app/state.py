import glob
import json
import logging
from datetime import datetime
from pathlib import Path

from link.config.models import AgentConfig, PipelineConfig

logger = logging.getLogger(__name__)


class StateManager:
    """
    Manages persistence of the Agent's entire configuration and runtime state.
    Serves as the Single Source of Truth for crash recovery.
    """

    # Stores state directly in the main configs folder
    STATE_DIR = Path("configs")

    def __init__(self):
        """Initialises the StateManager."""
        # We store the raw dicts to avoid constant Pydantic validation overhead during IO
        self._cache: dict | None = None
        self.current_session_path: Path | None = None

        # Ensure directory exists (it likely does for default_config.json)
        self.STATE_DIR.mkdir(parents=True, exist_ok=True)

    def load_state(self, explicit_path: Path | None = None) -> dict | None:
        """Loads state from disk.

        1. If explicit_path is provided, tries to load that specific file.
        2. If not, finds the most recent timestamped session file in configs/.

        Args:
            explicit_path (Path | None): Optional path to a specific state file.

        Returns:
            dict | None: The loaded state dictionary or None if loading failed.
        """
        if self._cache:
            return self._cache

        target_path = None

        # A. Explicit Request
        if explicit_path:
            if explicit_path.exists():
                target_path = explicit_path
            else:
                logger.warning(f"Requested state file not found: {explicit_path}")
                return None

        # B. Auto-Discovery (Latest)
        else:
            target_path = self._get_latest_session_file()

        if not target_path:
            return None

        try:
            data = json.loads(target_path.read_text(encoding="utf-8"))

            # Basic schema check: Version Compatibility
            # We strictly require version 2.1, but pipelines are optional.
            if data.get("version") == 2.1:
                self._cache = data
                self.current_session_path = target_path
                return data
            else:
                # Malformed or incompatible version
                logger.debug(f"Skipping file {target_path}: Version mismatch (Expected 2.1)")
                return None

        except Exception as e:
            logger.warning(f"State file corrupted ({target_path}): {e}")

    def bootstrap(self, config: AgentConfig):
        """Called on fresh start (or explicit config load) to seed a NEW session.

        Generates a timestamped filename in configs/.

        Args:
            config (AgentConfig): The initial agent configuration.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"session_{timestamp}.json"
        self.current_session_path = self.STATE_DIR / filename

        logger.info(f"Initialising new session state: {filename}")

        # Dump config to dict
        data = config.model_dump()

        # Ensure 'active' flag exists on all pipelines (Default: False)
        # This normalizes raw configs into state files
        if "pipelines" in data:
            for p in data["pipelines"]:
                if "active" not in p:
                    p["active"] = False

        self._cache = data
        self._flush()

    def update_pipeline_config(self, pipeline_config: PipelineConfig):
        """Updates (or adds) a pipeline configuration in the persistent state.

        Args:
            pipeline_config (PipelineConfig): The pipeline configuration to update.
        """
        self._ensure_loaded()
        assert self._cache is not None

        pipelines_list = self._cache.get("pipelines", [])

        # Convert new config to dict
        new_p_data = pipeline_config.model_dump()

        # Find and replace, or append
        found = False
        for i, p in enumerate(pipelines_list):
            if p["id"] == pipeline_config.id:
                # Preserve the existing 'active' state if replacing
                is_active = p.get("active", False)
                new_p_data["active"] = is_active
                pipelines_list[i] = new_p_data
                found = True
                break

        if not found:
            # New pipelines start inactive by default
            new_p_data["active"] = False
            pipelines_list.append(new_p_data)

        self._cache["pipelines"] = pipelines_list
        self._flush()

    def set_pipeline_status(self, pid: str, active: bool):
        """Marks a pipeline as active (running) or inactive (stopped).

        Args:
            pid (str): The pipeline ID.
            active (bool): The new activity status.
        """
        self._ensure_loaded()
        assert self._cache is not None

        pipelines_list = self._cache.get("pipelines", [])

        for p in pipelines_list:
            if p["id"] == pid:
                p["active"] = active
                break

        self._flush()

    def remove_pipeline(self, pid: str):
        """Completely removes a pipeline from config.

        Args:
            pid (str): The pipeline ID to remove.
        """
        self._ensure_loaded()
        assert self._cache is not None

        if "pipelines" in self._cache:
            self._cache["pipelines"] = [p for p in self._cache["pipelines"] if p["id"] != pid]

        self._flush()

    def _get_latest_session_file(self) -> Path | None:
        """Finds the most recent session file in configs/ based on timestamp.

        Returns:
            Path | None: The path to the latest session file or None if not found.
        """
        pattern = str(self.STATE_DIR / "session_*.json")
        files = glob.glob(pattern)
        if not files:
            return None
        return Path(sorted(files)[-1])

    def _ensure_loaded(self):
        """Ensures that the state is loaded from disk or initialised."""
        if self._cache is None:
            # If accessed before bootstrap/load, try to auto-load latest
            if not self.load_state():
                self._cache = {"identity": {}, "pipelines": []}

    def _flush(self):
        """Writes the current state cache to the active session file."""
        if self._cache and self.current_session_path:
            try:
                self.current_session_path.write_text(json.dumps(self._cache, indent=2))
            except Exception as e:
                logger.error(f"Failed to save state: {e}")
