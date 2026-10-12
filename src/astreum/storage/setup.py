from __future__ import annotations

import threading
from pathlib import Path
from typing import Any


def _level_0_size(table_dir: str | Path | None) -> int:
    """Sum the sizes of the ``*.bin`` files in a table's ``level_0``.
    
    Args:
        table_dir: The table directory path, or None.
    
    Returns:
        Total size in bytes, or 0 if the directory does not exist.
    """
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

    Returns: None
    """

    node.logger.info("Setting up node storage")

    node.hot_storage = {}
    node.hot_storage_timestamps = {}
    node.storage_index = {}
    node.startup_announce = None
    node.storage_providers = []
    # Which peer holds a recent block's uncommitted exprs (BLOCK message).
    from astreum.consensus.block.holders import init_block_holders

    init_block_holders(node)
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

    node.is_storage_provider = bool(config.get("is_storage_provider"))
    if node.is_storage_provider and not config.get("cold_storage_path"):
        node.logger.error(
            "is_storage_provider requires cold_storage_path; disabling storage provider"
        )
        node.is_storage_provider = False
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
