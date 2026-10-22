from __future__ import annotations

import atexit
import csv
import errno
import inspect
import gzip
import io
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

from astreum.utils.config import NodeConfig

# Fixed identity for all loggers in this library
_ORG_NAME = "Astreum"
_PRODUCT_NAME = "lib-py"
CSV_FIELDNAMES = (
    "ts",
    "level",
    "msg",
    "module",
    "func",
)
_RESOURCE_EXHAUSTION_ERRNOS = {errno.ENOSPC}

if hasattr(errno, "EDQUOT"):
    _RESOURCE_EXHAUSTION_ERRNOS.add(errno.EDQUOT)
if hasattr(errno, "ENOMEM"):
    _RESOURCE_EXHAUSTION_ERRNOS.add(errno.ENOMEM)


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


def _record_payload(record: logging.LogRecord) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
        "level": record.levelname,
        "msg": record.getMessage(),
        "module": record.module,
        "func": record.funcName,
    }
    return payload


def _is_resource_exhaustion_error(exc: BaseException | None) -> bool:
    if isinstance(exc, MemoryError):
        return True
    if not isinstance(exc, OSError):
        return False
    return exc.errno in _RESOURCE_EXHAUSTION_ERRNOS


class CSVFormatter(logging.Formatter):
    """Log record formatter that emits CSV rows."""

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        payload = _record_payload(record)

        line = io.StringIO()
        writer = csv.writer(line)
        writer.writerow(
            [
                payload["ts"],
                payload["level"],
                payload["msg"],
                payload["module"],
                payload["func"],
            ]
        )
        return line.getvalue().rstrip("\r\n")


class CSVTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Timed rotating file handler that keeps a CSV header in every active file."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._disabled = False

    def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
        if self._disabled:
            return
        super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:  # type: ignore[override]
        exc_type, exc, _ = logging.sys.exc_info()
        if _is_resource_exhaustion_error(exc):
            self._disabled = True
            try:
                self.close()
            except Exception:
                pass
            return
        if exc_type is not None:
            super().handleError(record)

    def _open(self):  # type: ignore[override]
        stream = open(
            self.baseFilename,
            self.mode,
            encoding=self.encoding,
            errors=self.errors,
            newline="",
        )
        if stream.tell() == 0:
            csv.writer(stream).writerow(CSV_FIELDNAMES)
            stream.flush()
        return stream


def _gzip_rotator(src: str, dst: str) -> None:
    """Rotate the log file by gzipping it and removing the original.

    Args:
        src: Path to the source log file.
        dst: Destination path for the gzipped file.

    Returns: None
    """
    with open(src, "rb") as source, gzip.open(f"{dst}.gz", "wb") as target:
        shutil.copyfileobj(source, target)
    os.remove(src)


def _namer(default_name: str) -> str:
    """Custom name for rotated logs: node-YYYY-MM-DD.ext.

    Args:
        default_name: Default rotation filename from the handler.

    Returns:
        Reformatted filename with date appended before the extension.
    """
    path = pathlib.Path(default_name)
    parent = path.parent
    name = path.name
    suffix = path.suffix
    marker = f"{suffix}."
    if marker not in name:
        return default_name
    stem, date_part = name.rsplit(marker, 1)
    return str(parent / f"{stem}-{date_part}{suffix}")


def _remove_legacy_recent_log(log_dir: pathlib.Path) -> None:
    """Delete the previous JSON-lines recent log if it is still present."""
    legacy_file = log_dir / "node.log"
    try:
        if legacy_file.is_file():
            legacy_file.unlink()
    except OSError:
        pass


def _human_line(record: logging.LogRecord) -> str:
    """Format a record as a concise human-readable line.

    Args:
        record: LogRecord to format.

    Returns:
        Formatted human-readable log line.
    """
    dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
    stamp = f"{dt:%Y-%m-%d}-{dt:%S}-{dt:%M}"
    prefix = getattr(record, "logger_name", None)
    if prefix:
        return f"[{stamp}] [{record.levelname.lower()}] {prefix}: {record.getMessage()}"
    return f"[{stamp}] [{record.levelname.lower()}] {record.getMessage()}"


class HumanFormatter(logging.Formatter):
    """Simple formatter for optional verbose console output."""

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        return _human_line(record)


def _shutdown_listener(listener: logging.handlers.QueueListener, handlers: list[logging.Handler]) -> None:
    """Stop the queue listener and close handlers on interpreter exit.

    Args:
        listener: QueueListener to stop.
        handlers: List of handlers to close.

    Returns: None
    """
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


def _disabled_logger(product: str, instance_id: str, logger_name: str | None) -> logging.LoggerAdapter:
    """Return a logger adapter that silently drops all log records.

    Args:
        product: Product name for logger namespace.
        instance_id: Instance identifier for logger namespace.
        logger_name: Optional logger name for extra context.

    Returns:
        LoggerAdapter with NullHandler that drops all records.
    """
    base_logger = logging.getLogger(f"{product}.{instance_id}.disabled")
    base_logger.setLevel(logging.INFO)
    base_logger.handlers.clear()
    base_logger.propagate = False
    base_logger.addHandler(logging.NullHandler())
    return logging.LoggerAdapter(
        base_logger,
        {"instance_id": instance_id, "logger_name": logger_name},
    )


def logging_setup(config: NodeConfig) -> logging.LoggerAdapter:
    """Configure logging according to the runtime config and return an adapter.

    Args:
        config: Node configuration. Fields used:
            - logging_retention_days: Number of days to retain log files.
            - logging_enabled: Boolean to enable/disable logging.
            - logger_name: Optional logger name for extra context.
            - verbose: Boolean to enable console output.

    Returns:
        LoggerAdapter instance for logging.
    """
    org = _ORG_NAME
    product = _PRODUCT_NAME
    instance_id = _derive_instance_id()

    retention_days = config.logging_retention_days

    logging_enabled = config.logging_enabled
    logger_name = config.logger_name
    if not logging_enabled:
        return _disabled_logger(product, instance_id, logger_name)

    verbose = config.verbose

    log_dir = _log_root(org, product, instance_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    _remove_legacy_recent_log(log_dir)

    base_file = log_dir / "node.csv"
    file_handler = CSVTimedRotatingFileHandler(
        filename=str(base_file),
        when="midnight",
        interval=1,
        backupCount=max(retention_days, 0),
        utc=True,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(CSVFormatter())
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

    extra = {"instance_id": instance_id, "logger_name": logger_name}
    adapter = logging.LoggerAdapter(base_logger, extra)
    setattr(adapter, "_queue_listener", listener)
    setattr(adapter, "_handlers", handler_list)

    return adapter


__all__ = [
    "CSVFormatter",
    "HumanFormatter",
    "logging_setup",
]
