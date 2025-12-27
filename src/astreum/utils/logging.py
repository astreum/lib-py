from __future__ import annotations

import atexit
import inspect
import gzip
import json
import logging
import logging.handlers
import os
import pathlib
import platform
import queue
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from blake3 import blake3

# Fixed identity for all loggers in this library
_ORG_NAME = "Astreum"
_PRODUCT_NAME = "lib-py"


def _safe_path(path_str: str) -> Optional[pathlib.Path]:
    try:
        return pathlib.Path(path_str).resolve()
    except Exception:
        try:
            return pathlib.Path(path_str).absolute()
        except Exception:
            return None


def _hash_path(path: pathlib.Path) -> str:
    try:
        data = str(path).encode("utf-8", errors="ignore")
    except Exception:
        data = repr(path).encode("utf-8", errors="ignore")
    return blake3(data).hexdigest()


def _find_caller_path() -> pathlib.Path:
    stack = inspect.stack()
    candidates: list[pathlib.Path] = []
    for frame_info in stack[2:]:
        filename = frame_info.filename
        if not filename:
            continue
        path = _safe_path(filename)
        if path is None:
            continue
        candidates.append(path)
        if "astreum" not in path.parts:
            return path

    if candidates:
        return candidates[0]
    return pathlib.Path.cwd()


def _derive_instance_id() -> str:
    return _hash_path(_find_caller_path())[:16]


def _log_root(org: str, product: str, instance_id: str) -> pathlib.Path:
    """Resolve the base directory for logs using platform defaults."""
    if platform.system() == "Windows":
        base = os.getenv("LOCALAPPDATA") or str(pathlib.Path.home())
        return pathlib.Path(base) / org / product / "logs" / instance_id

    xdg_state = os.getenv("XDG_STATE_HOME")
    base_path = pathlib.Path(xdg_state) if xdg_state else pathlib.Path.home() / ".local" / "state"
    return base_path / org / product / "logs" / instance_id


class JSONFormatter(logging.Formatter):
    """Log record formatter that emits JSON objects per line."""

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "pid": record.process,
            "thread": record.threadName,
            "module": record.module,
            "func": record.funcName,
            "instance_id": getattr(record, "instance_id", None),
        }

        for key, value in record.__dict__.items():
            if key in payload or key.startswith(("_", "msecs", "relativeCreated")):
                continue
            try:
                json.dumps(value)
            except Exception:
                continue
            payload[key] = value

        return json.dumps(payload, ensure_ascii=False)


def _gzip_rotator(src: str, dst: str) -> None:
    """Rotate the log file by gzipping it and removing the original."""
    with open(src, "rb") as source, gzip.open(f"{dst}.gz", "wb") as target:
        shutil.copyfileobj(source, target)
    os.remove(src)


def _namer(default_name: str) -> str:
    """Custom name for rotated logs: node-YYYY-MM-DD.log."""
    path = pathlib.Path(default_name)
    parent = path.parent
    name = path.name
    fragments = name.split(".log.")
    if len(fragments) != 2:
        return default_name
    stem, date_part = fragments
    return str(parent / f"{stem}-{date_part}.log")


def _human_line(record: logging.LogRecord) -> str:
    """Format a record as a concise human-readable line."""
    dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
    stamp = f"{dt:%Y-%m-%d}-{dt:%S}-{dt:%M}"
    return f"[{stamp}] [{record.levelname.lower()}] {record.getMessage()}"


class HumanFormatter(logging.Formatter):
    """Simple formatter for optional verbose console output."""

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        return _human_line(record)


def _shutdown_listener(listener: logging.handlers.QueueListener, handlers: list[logging.Handler]) -> None:
    """Stop the queue listener and close handlers on interpreter exit."""
    try:
        listener.stop()
    except Exception:
        pass
    finally:
        for handler in handlers:
            try:
                handler.close()
            except Exception:
                pass


def logging_setup(config: dict) -> logging.LoggerAdapter:
    """Configure logging according to the runtime config and return an adapter."""
    if config is None:
        config = {}
    elif not isinstance(config, dict):
        config = dict(config)

    org = _ORG_NAME
    product = _PRODUCT_NAME
    instance_id = _derive_instance_id()

    retention_value = config.get("logging_retention_days")
    retention_days = int(retention_value) if retention_value is not None else 90

    verbose = bool(config.get("verbose", False))

    log_dir = _log_root(org, product, instance_id)
    log_dir.mkdir(parents=True, exist_ok=True)

    base_file = log_dir / "node.log"
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(base_file),
        when="midnight",
        interval=1,
        backupCount=max(retention_days, 0),
        utc=True,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(JSONFormatter())
    file_handler.rotator = _gzip_rotator
    file_handler.namer = _namer

    handler_list: list[logging.Handler] = [file_handler]

    if verbose:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(HumanFormatter())
        handler_list.append(console_handler)

    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(-1)
    queue_handler = logging.handlers.QueueHandler(log_queue)

    base_logger = logging.getLogger(f"{product}.{instance_id}")
    base_logger.setLevel(logging.INFO)
    base_logger.handlers.clear()
    base_logger.propagate = False
    base_logger.addHandler(queue_handler)

    listener = logging.handlers.QueueListener(
        log_queue, *handler_list, respect_handler_level=True
    )
    listener.daemon = True
    listener.start()
    atexit.register(_shutdown_listener, listener, handler_list)

    adapter = logging.LoggerAdapter(base_logger, {"instance_id": instance_id})
    setattr(adapter, "_queue_listener", listener)
    setattr(adapter, "_handlers", handler_list)

    return adapter


__all__ = [
    "HumanFormatter",
    "JSONFormatter",
    "logging_setup",
]
