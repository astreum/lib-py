from __future__ import annotations

import time
from typing import TYPE_CHECKING

from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.storage.advertisements import advertise_exprs
from astreum.storage.radix import RadixTree, get_from_radix_tree
from astreum.storage.records import (
    fetch_and_store_record,
    get_record_from_cold_storage,
    parse_record_new_count,
)

if TYPE_CHECKING:
    from astreum.node import Node


_PRICE_MOVEMENT_TABLE: tuple[tuple[float, float], ...] = (
    (0.10, 0.60),  # -40%
    (0.20, 0.70),  # -30%
    (0.30, 0.80),  # -20%
    (0.40, 0.90),  # -10%
    (0.50, 0.96),  # -4%
    (0.60, 1.04),  # +4%
    (0.70, 1.12),  # +12%
    (0.80, 1.30),  # +30%
    (0.90, 1.70),  # +70%
    (1.00, 2.50),  # +150%
)


def _clamp01(value: float) -> float:
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return value


def _incoming_queue_pressure(node: "Node") -> float:
    with node.incoming_queue_size_lock:
        size = int(node.incoming_queue_size or 0)
        limit = int(node.incoming_queue_size_limit or 0)

    if limit <= 0:
        return 0.0
    return _clamp01(size / limit)


def _price_movement_factor_for_pressure(pressure: float) -> float:
    p = _clamp01(float(pressure))
    for threshold, factor in _PRICE_MOVEMENT_TABLE:
        if p <= threshold:
            return factor
    return _PRICE_MOVEMENT_TABLE[-1][1]


def _update_storage_request_price(node: "Node") -> None:
    pressure = _incoming_queue_pressure(node)
    factor = _price_movement_factor_for_pressure(pressure)
    previous_price = node.storage_request_current_price
    target_price = max(node.config["storage_request_minimum_price"], round(previous_price * factor))

    
    node.storage_request_current_price = target_price
    if previous_price != target_price:
        node.logger.debug(
            "Storage request price updated (price=%s prev=%s min=%s pressure=%.3f factor=%.3f)",
            target_price,
            previous_price,
            pressure,
            factor,
        )


def _long_term_store_one(node: "Node") -> bool:
    """Process one storage-index entry per call: skip if already in the
    records table, otherwise fetch the data and store it (cold + records)."""
    if not node.long_term_storage:
        return False

    keys = list(node.storage_index.keys())  # snapshot; UDP thread mutates the dict
    if not keys:
        return False
    cursor = node.long_term_cursor % len(keys)
    node.long_term_cursor = cursor + 1
    storage_id = keys[cursor]

    if get_record_from_cold_storage(node, storage_id) is not None:
        return False  # already stored

    block = getattr(node, "latest_block", None)
    if block is None:
        return False
    storage_account = block.accounts.get_account(STORAGE_ADDRESS, node)
    if storage_account is None or storage_account.data.root_hash is None:
        return False

    tree = RadixTree(root_hash=storage_account.data.root_hash)  # fresh per expr: bounded memory
    new_count = parse_record_new_count(node, get_from_radix_tree(tree, node, storage_id))
    if new_count is None:
        return False

    return fetch_and_store_record(node, storage_id, tree, new_count)


def advertise_storage(astreum_node: "Node") -> None:
    """Periodically update the storage request price and long-term-store.

    Runs a single timed loop in a daemon thread that adjusts
    ``storage_request_current_price`` based on incoming queue pressure to
    signal demand.  When ``long_term_storage`` is enabled, the same loop also
    processes one storage-index entry per ``long_term_storage_interval``
    seconds: entries missing from the records table are fetched and written
    to cold storage plus the records table.  Fresh-expr advertisement happens
    at block creation via ``advertise_exprs(entries=...)``; the periodic
    re-advertise loop was removed.

    The thread exits when ``communication_stop_event`` is set.

    Args:
        astreum_node: A Node instance with storage and communication infrastructure
            already initialized (``setup_storage`` must have been called).
    """
    price_interval = float(astreum_node.config.get("storage_request_price_interval") or 0)
    if price_interval <= 0:
        astreum_node.logger.info("Storage advertiser disabled (no price interval configured)")
        return

    long_term_interval = float(
        getattr(astreum_node, "long_term_storage_interval", 0) or 0
    )
    long_term_enabled = bool(getattr(astreum_node, "long_term_storage", False))

    if long_term_enabled:
        astreum_node.logger.info(
            "Storage advertiser started (price_interval=%ss, long_term_interval=%ss)",
            price_interval,
            long_term_interval,
        )
    else:
        astreum_node.logger.info(
            "Storage advertiser started (price_interval=%ss)",
            price_interval,
        )
    stop = astreum_node.communication_stop_event
    now = time.monotonic()
    next_price_at = now if price_interval > 0 else None
    next_long_term_at = now if long_term_enabled and long_term_interval > 0 else None

    while not stop.is_set():
        now = time.monotonic()

        if next_price_at is not None and now >= next_price_at:
            try:
                _update_storage_request_price(astreum_node)
            except Exception as exc:
                astreum_node.logger.exception("Storage request price update failed: %s", exc)
            while next_price_at <= now:
                next_price_at += price_interval

        if next_long_term_at is not None and now >= next_long_term_at:
            try:
                _long_term_store_one(astreum_node)
            except Exception as exc:
                astreum_node.logger.exception("Long-term storage step failed: %s", exc)
            while next_long_term_at <= now:
                next_long_term_at += long_term_interval

        deadlines = [d for d in (next_price_at, next_long_term_at) if d is not None]
        if not deadlines:
            stop.wait(1.0)
            continue
        wait_timeout = max(0.0, min(deadlines) - now)
        if stop.wait(wait_timeout):
            break

    astreum_node.logger.info("Storage advertiser stopped")
