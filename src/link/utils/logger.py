# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Structured logging — LinkReporter, async handlers, route-keyed event dispatch."""

import json
import logging
import queue
import sys
import threading
import time
from typing import Any

import requests
from pydantic import BaseModel
from typing_extensions import override

from link.utils.version import resolve_agent_version

_SEVERITY_MAP = {
    "DEBUG": "info",
    "INFO": "info",
    "WARNING": "warning",
    "ERROR": "error",
    "CRITICAL": "critical",
}

# Maps logger-name prefixes to default categories for the backend's LogCreate schema.
_CATEGORY_BY_MODULE: list[tuple[str, str]] = [
    ("link.app.onboarding", "authentication"),
    ("link.app.updater", "deployment"),
    ("link.app.reconfigure", "configuration"),
    ("link.app.state", "configuration"),
    ("link.config", "configuration"),
    ("link.app.runtime", "execution"),
    ("link.components", "execution"),
    ("link.adapters", "health"),
    ("link.infra", "system"),
    ("link.utils", "system"),
    ("link_language_model", "inference"),
    ("link_audio_transcriber", "inference"),
    ("link_image_classifier", "inference"),
    ("link_audio_classifier", "inference"),
]


class CategoryFilter(logging.Filter):
    """Injects a default `category` on every record based on its logger name.

    Records that already carry a `category` (from `extra={"category": "..."}`)
    are left untouched, so per-call overrides win over module defaults.
    """

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "category"):
            for prefix, cat in _CATEGORY_BY_MODULE:
                if record.name.startswith(prefix):
                    record.category = cat
                    break
        return True


_category_filter = CategoryFilter()

try:
    import zenoh
except ImportError:
    zenoh = None


class LinkReporter(logging.Logger):
    """Custom Logger that provides high-level reporting methods."""

    def report_lifecycle(self, status: str):
        """Reports Agent Online/Offline status.

        Args:
            status (str): The lifecycle status ('online' or 'offline').
        """
        payload: dict[str, Any] = {"status": status}
        v = resolve_agent_version()
        if v:
            payload["agent_version"] = v
        if status == "offline":
            payload["metrics"] = {"cpu_usage": 0, "ram_usage": 0, "temperature_celsius": 0, "storage_available_gb": 0}

        # Explicitly use "lifecycle_status" to avoid confusion with standard logs
        self.info(payload, extra={"route_key": "lifecycle_status"})

    def report_command(self, cmd_id: str, status: str, output: str):
        """Reports command execution results.

        Args:
            cmd_id (str): The command ID.
            status (str): The execution status (e.g., 'completed', 'failed').
            output (str): The command output or error message.
        """
        if not cmd_id:
            return
        payload = {"status": status, "output": str(output)}

        self.info(payload, extra={"route_key": "command_status", "context": {"cid": cmd_id}})

    def report_model(self, model_id: str, **kwargs: Any):
        """Reports model state.

        Args:
            model_id (str): The model/pipeline ID.
            **kwargs (Any): Additional model status attributes.
        """
        if not model_id:
            return
        payload = {k: v for k, v in kwargs.items() if v is not None}

        if payload:
            self.info(payload, extra={"route_key": "model_status", "context": {"mid": model_id}})

    def report_deployment_progress(
        self,
        model_id: str,
        stage: str,
        progress_pct: float,
        bytes_done: int = 0,
        total_bytes: int = 0,
    ) -> None:
        """Reports incremental deployment progress for a model.

        Args:
            model_id (str): The model/pipeline ID.
            stage (str): Deployment stage (`downloading`, `configuring`, `completed`, ...).
            progress_pct (float): Completion percentage 0-100.
            bytes_done (int): Bytes processed so far (download/extract). Defaults to 0.
            total_bytes (int): Total bytes expected. 0 when unknown.
        """
        if not model_id:
            return
        payload = {
            "stage": stage,
            "progress_pct": progress_pct,
            "bytes_done": bytes_done,
            "total_bytes": total_bytes,
        }
        self.info(payload, extra={"route_key": "deployment_progress", "context": {"mid": model_id}})


# Register BEFORE defining handlers
logging.setLoggerClass(LinkReporter)


class AsyncHandler(logging.Handler):
    """Base class for non-blocking handlers with template support."""

    def __init__(self, templates: dict[str, str]):
        super().__init__()
        self.templates = templates
        self.queue = queue.Queue(maxsize=1000)
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    @override
    def emit(self, record):
        if self._stop_event.is_set():
            return
        try:
            route_key = getattr(record, "route_key", "logs")
            context = getattr(record, "context", {})
            template = self.templates.get(route_key)

            # Fallback for standard logs: if key is "logs" but no template named "logs",
            if not template and route_key == "logs":
                template = self.templates.get("url") or self.templates.get("topic")

            if not template:
                return

            try:
                target = template.format(**context)
            except KeyError as e:
                print(f"❌ CONFIG ERROR: Template requires {e}, but context has {list(context.keys())}")
                print(f"   Route: {route_key}")
                print(f"   Template: {template}")
                return

            if isinstance(record.msg, dict):
                # Structured data (Reporter) -> Send as JSON
                raw_payload = record.msg
                payload = json.dumps(raw_payload, default=str)
            else:
                # Text log -> shape to backend LogCreate schema
                raw_payload = {
                    "message": record.getMessage(),
                    "severity": _SEVERITY_MAP.get(record.levelname, "info"),
                    "category": getattr(record, "category", "other"),
                }
                payload = json.dumps(raw_payload, default=str)

            self.queue.put_nowait((target, payload, raw_payload, route_key))
        except queue.Full:
            pass
        except Exception:
            self.handleError(record)

    def _worker_loop(self):
        while not self._stop_event.is_set():
            try:
                target, payload, raw_payload, route_key = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._transport_emit(target, payload, raw_payload, route_key)
            except Exception as e:
                sys.stderr.write(f"Link Logger Error ({route_key}): {e}\n")
            self.queue.task_done()

        while True:
            try:
                target, payload, raw_payload, route_key = self.queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._transport_emit(target, payload, raw_payload, route_key)
            except Exception as e:
                sys.stderr.write(f"Link Logger Error ({route_key}) [drain]: {e}\n")
            self.queue.task_done()

    def _transport_emit(self, target, payload, raw_payload, route_key):
        pass

    @override
    def close(self):
        self._stop_event.set()
        if self._worker.is_alive():
            self._worker.join(timeout=2.0)
        super().close()


class AsyncZenohHandler(AsyncHandler):
    """Logging handler that publishes logs to Zenoh asynchronously."""

    def __init__(self, session: Any, args: dict[str, Any]):
        """Initialises the Zenoh handler.

        Args:
            session (Any): The Zenoh session.
            args (dict): Configuration arguments (templates).
        """
        self.session = session
        templates = {k: str(v) for k, v in args.items()}
        super().__init__(templates)

    @override
    def _transport_emit(self, target: str, payload: str, raw_payload: Any, route_key: str):
        """Emits the log payload to Zenoh.

        Args:
            target (str): The Zenoh key expression.
            payload (str): The JSON payload.
            raw_payload (Any): The raw log record data.
            route_key (str): The routing key (log type).
        """
        if self.session:
            try:
                self.session.put(target, payload)
            except Exception as e:
                if "closed" in str(e).lower():
                    return
                raise e


class AsyncHTTPHandler(AsyncHandler):
    """Logging handler that sends logs via HTTP requests asynchronously."""

    _MAX_RETRIES = 2  # 3 total attempts (initial + 2 retries)

    def __init__(self, args: dict[str, Any]):
        """Initialises the HTTP handler.

        Args:
            args (dict): Configuration arguments (api_key, timeout, templates).
        """
        self.headers = {}
        if args.get("api_key"):
            self.headers["Authorization"] = f"Bearer {args['api_key']}"

        read_timeout = float(args.get("timeout", 10))
        self.timeout: tuple[float, float] = (3.0, read_timeout)

        templates = {k: str(v) for k, v in args.items()}
        super().__init__(templates)

    @override
    def _transport_emit(self, target: str, payload: str, raw_payload: Any, route_key: str):
        """Emits the log payload via HTTP POST/PUT with bounded retry.

        Retries timeouts, connection errors, and 5xx responses with exponential
        backoff. 4xx responses are fatal — raised immediately so the worker logs
        them once and moves on.

        Args:
            target (str): The target URL.
            payload (str): The JSON string payload.
            raw_payload (Any): The raw log record data.
            route_key (str): The routing key (log type).
        """
        method = requests.put if route_key == "lifecycle_status" else requests.post
        json_data = raw_payload if isinstance(raw_payload, dict) else None
        data = payload if not json_data else None

        for attempt in range(self._MAX_RETRIES + 1):
            retryable_err: Exception
            try:
                resp = method(target, json=json_data, data=data, headers=self.headers, timeout=self.timeout)
                if resp.status_code < 500:
                    # 2xx success or 4xx fatal — raise_for_status handles both.
                    resp.raise_for_status()
                    return
                retryable_err = requests.HTTPError(f"{resp.status_code} {resp.reason}")
            except (requests.Timeout, requests.ConnectionError) as e:
                retryable_err = e

            if attempt >= self._MAX_RETRIES:
                raise retryable_err
            time.sleep(0.5 * (3**attempt))  # 0.5s, 1.5s


class CleanFormatter(logging.Formatter):
    """Formatter for machine-readable transports — serialises dict records as JSON."""

    @override
    def format(self, record):
        """Render the record, converting dict messages to JSON strings first."""
        if isinstance(record.msg, dict):
            record.msg = json.dumps(record.msg, default=str)
        return super().format(record)


class PrettyFormatter(logging.Formatter):
    """Formatter for console output — prefixes records with severity-icon emoji."""

    ICONS = {logging.INFO: "ℹ️", logging.WARNING: "⚠️", logging.ERROR: "⛔️", logging.CRITICAL: "📛"}

    @override
    def format(self, record):
        """Render the record with icon prefix (text) or 📡 emoji (dict payloads)."""
        original_msg = record.msg
        if isinstance(record.msg, dict):
            record.msg = f"📡 {json.dumps(record.msg, default=str)}"
        else:
            icon = self.ICONS.get(record.levelno, "")
            if icon:
                record.msg = f"{icon}  {record.msg}"
        result = super().format(record)
        record.msg = original_msg
        return result


def setup_logging(
    logging_config: Any = None, reporting_config: Any = None, zenoh_session: Any = None
) -> logging.Logger:
    """Configures the root logger and the special reporter logger.

    Args:
        logging_config (Any): Configuration for general logging.
        reporting_config (Any): Configuration for status reporting.
        zenoh_session (Any): Optional Zenoh session for transport.

    Returns:
        logging.Logger: The configured root logger.
    """
    _configure_logger(None, logging_config, zenoh_session)
    _configure_logger("link.reporter", reporting_config, zenoh_session)
    return logging.getLogger()


def rebuild_handlers(logging_config: Any, reporting_config: Any, zenoh_session: Any = None) -> None:
    """Tear down existing handlers and reattach from new configs.

    Safe to call while pipelines are running — the root logger reference is
    preserved; only its handler set is swapped. AsyncHandler worker threads
    are stopped via `.close()` before the handlers are dropped.

    Args:
        logging_config: New `LoggingConfig` (or dict).
        reporting_config: New `ReportingConfig` (or dict).
        zenoh_session: Optional Zenoh session, reused across the swap.
    """
    for lg_name in (None, "link.reporter"):
        lg = logging.getLogger(lg_name)
        for h in list(lg.handlers):
            try:
                h.close()
            except Exception:
                pass
        lg.handlers.clear()
    _configure_logger(None, logging_config, zenoh_session)
    _configure_logger("link.reporter", reporting_config, zenoh_session)


def _configure_logger(name, config, session):
    logger = logging.getLogger(name)
    if logger.handlers:
        logger.handlers.clear()

    if name == "link.reporter":
        logger.propagate = False

    handlers = getattr(config, "handlers", []) if config else []
    level_str = getattr(config, "level", "INFO") if config else "INFO"
    target_level = getattr(logging, level_str.upper(), logging.INFO)

    logger.setLevel(target_level)

    if not name and not handlers:
        handlers = [{"type": "console"}]

    for h in handlers:
        h_data = h.model_dump() if isinstance(h, BaseModel) else h
        h_type = h_data.get("type", "").lower()
        args = h_data.get("args", {})

        if not isinstance(args, dict):
            args = {}

        # Per-handler level override: args.level ("DEBUG"|"INFO"|"WARNING"|"ERROR").
        # When absent, the handler inherits the parent logger's level.
        handler_level = target_level
        if "level" in args:
            handler_level = getattr(logging, str(args["level"]).upper(), target_level)

        if h_type == "console":
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(PrettyFormatter("%(message)s"))
            console.setLevel(handler_level)
            console.addFilter(_category_filter)
            logger.addHandler(console)

        elif h_type.startswith("zenoh") and zenoh and session:
            z = AsyncZenohHandler(session, args)
            z.setFormatter(CleanFormatter())
            z.setLevel(handler_level)
            z.addFilter(_category_filter)
            logger.addHandler(z)

        elif h_type == "http":
            h = AsyncHTTPHandler(args)
            h.setFormatter(CleanFormatter())
            h.setLevel(handler_level)
            h.addFilter(_category_filter)
            logger.addHandler(h)
