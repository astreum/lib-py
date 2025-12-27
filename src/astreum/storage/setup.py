from __future__ import annotations

from typing import Any


def storage_setup(node: Any, config: dict) -> None:
    """Initialize hot/cold storage helpers on the node."""

    node.logger.info("Setting up node storage")

    node.hot_storage = {}
    node.hot_storage_hits = {}
    node.storage_index = {}
    node.hot_storage_size = 0
    node.cold_storage_size = 0

    node.logger.info(
        "Storage ready (hot_limit=%s bytes, cold_limit=%s bytes, cold_path=%s)",
        config["hot_storage_limit"],
        config["cold_storage_limit"],
        config["cold_storage_path"] or "disabled",
    )
