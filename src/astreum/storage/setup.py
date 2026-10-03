from __future__ import annotations

import threading
from pathlib import Path
from typing import Any


def _level_0_size(table_dir: str | Path | None) -> int:
    """Sum the sizes of the ``*.bin`` files in a table's ``level_0``."""
    if not table_dir:
        return 0
    level_0_path = Path(table_dir) / "level_0"
    if not level_0_path.exists() or not level_0_path.is_dir():
        return 0
    total = 0
    for expr_path in level_0_path.glob("*.bin"):
        try:
            total += expr_path.stat().st_size
        except OSError:
            return 0
    return total


def setup_storage(node: Any, config: dict) -> None:
    """Initialize hot and cold storage infrastructure on a Node.

    Creates the in-memory hot storage cache, initializes the cold
    storage directory structure on disk (``level_0`` in each table
    directory), and measures the existing cold storage size for
    capacity tracking.

    Args:
        node: A Node instance to attach storage attributes to.
        config: The node configuration dict, expected to contain
            ``hot_storage_limit``, ``cold_storage_path``,
            ``cold_storage_limit``, ``storage_fetch_interval``,
            and ``storage_fetch_retries``.
    """

    node.logger.info("Setting up node storage")

    node.hot_storage = {}
    node.hot_storage_timestamps = {}
    node.storage_index = {}
    node.storage_providers = []
    node.uncommitted_block = None
    node.claim_spacing_eras = {}
    node.cold_storage_lock = threading.RLock()
    node.hot_storage_lock = threading.RLock()
    node.hot_storage_size = 0
    node.cold_storage_size = 0
    node.storage_fetch_interval = config["storage_fetch_interval"]
    node.storage_fetch_retries = config["storage_fetch_retries"]

    cold_path = config.get("cold_storage_path")
    if cold_path:
        try:
            cold_root = Path(cold_path)
            cold_root.mkdir(parents=True, exist_ok=True)
            (cold_root / "exprs" / "level_0").mkdir(parents=True, exist_ok=True)
            (cold_root / "records" / "level_0").mkdir(parents=True, exist_ok=True)
        except OSError:
            node.logger.warning("Disabling cold storage; unable to create tables in %s", cold_path)
            config["cold_storage_path"] = None

    node.cold_storage_level_0_size = _level_0_size(
        Path(config["cold_storage_path"]) / "exprs" if config.get("cold_storage_path") else None
    )
    node.records_level_0_size = _level_0_size(
        Path(config["cold_storage_path"]) / "records" if config.get("cold_storage_path") else None
    )

    node.long_term_storage = bool(config.get("long_term_storage"))
    if node.long_term_storage and not config.get("cold_storage_path"):
        node.logger.error(
            "long_term_storage requires cold_storage_path; disabling long-term storage"
        )
        node.long_term_storage = False
    node.long_term_storage_interval = config["long_term_storage_interval"]
    node.long_term_cursor = 0

    node.logger.info(
        "Storage ready (hot_limit=%s bytes, cold_limit=%s bytes, cold_path=%s, storage_fetch_interval=%s, storage_fetch_retries=%s)",
        config["hot_storage_limit"],
        config["cold_storage_limit"],
        config["cold_storage_path"] or "disabled",
        config["storage_fetch_interval"],
        config["storage_fetch_retries"],
    )
