from __future__ import annotations

import time
from itertools import islice
from typing import TYPE_CHECKING

from astreum.consensus.constants import PROVIDERS_PER_RECORD, STORAGE_ADDRESS
from astreum.communication.models.peer import get_peer
from astreum.communication.self_lookup import SelfLookupTimer, self_lookup_timer_step
from astreum.communication.storage_request.code import StorageRequestCode
from astreum.communication.storage_request.model import StorageRequest
from astreum.communication.storage_response.storage_provider import decode_storage_provider
from astreum.expression import RESOLUTION_SINGLE
from astreum.storage.advertisements import advertise_exprs
from astreum.storage.exprs.network import (
    _send_storage_put,
    put_exprs_in_network,
    send_put_entries,
)
from astreum.storage.providers import provider_payload_for_id
from astreum.storage.radix import RadixTree, get_from_radix_tree
from astreum.storage.requests import prune_expired_expr_reqs
from astreum.storage.records import (
    fetch_and_store_record,
    get_record_from_cold_storage,
    iter_records_in_cold_storage,
    parse_record,
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
    """Clamp a value to the range [0.0, 1.0].

    Args:
        value: The value to clamp.

    Returns:
        The clamped value.
    """
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return value


def _incoming_queue_pressure(node: "Node") -> float:
    """Compute the incoming queue pressure as a ratio of size to limit.

    Args:
        node: A Node instance with incoming queue metrics.

    Returns:
        Queue pressure in range [0.0, 1.0].
    """
    with node.incoming_queue_size_lock:
        size = int(node.incoming_queue_size or 0)
        limit = int(node.incoming_queue_size_limit or 0)

    if limit <= 0:
        return 0.0
    return _clamp01(size / limit)


def _price_movement_factor_for_pressure(pressure: float) -> float:
    """Return the price adjustment factor for a given queue pressure.

    Args:
        pressure: Queue pressure in range [0.0, 1.0].

    Returns:
        Price movement factor (e.g., 1.04 for +4%, 0.96 for -4%).
    """
    p = _clamp01(float(pressure))
    for threshold, factor in _PRICE_MOVEMENT_TABLE:
        if p <= threshold:
            return factor
    return _PRICE_MOVEMENT_TABLE[-1][1]


def _update_storage_request_price(node: "Node") -> None:
    """Adjust storage request price based on queue pressure.

    Args:
        node: A Node instance with storage request pricing state.

    Returns: None
    """
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
    """Process one storage-index entry per call.

    Skips if already in the records table, otherwise fetches the data
    and stores it (cold + records).

    Args:
        node: A Node instance with storage and ``is_storage_provider`` enabled.

    Returns:
        True if an entry was processed, False otherwise.
    """
    if not node.is_storage_provider:
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

    tree = RadixTree(
        root_hash=storage_account.data.root_hash, root_block=storage_account.data.root_block
    )  # fresh per expr: bounded memory
    record = parse_record(node, get_from_radix_tree(tree, node, storage_id))
    if record is None:
        return False

    return fetch_and_store_record(node, storage_id, record)


_ANNOUNCE_DONE = "done"
_ANNOUNCE_MORE = "more"
_ANNOUNCE_WAIT = "wait"


def _startup_announce_step(node: "Node") -> str:
    """Announce one batch of stored records as this node.

    Does nothing until ``node.latest_block`` is loaded. The first ready call
    starts an iterator over the records table. Each call then sends the next
    batch, sized to fit one ``STORAGE_PUT`` datagram, through
    ``put_exprs_in_network``. An error ends the job.

    Args:
        node: A Node instance with ``is_storage_provider`` set.

    Returns:
        ``"wait"`` if the node is not synced yet, ``"more"`` if records
        remain, ``"done"`` if the job is finished or does not apply.
    """
    if not getattr(node, "is_storage_provider", False):
        return _ANNOUNCE_DONE
    state = getattr(node, "startup_announce", None)
    if state == _ANNOUNCE_DONE:
        return _ANNOUNCE_DONE
    if getattr(node, "latest_block", None) is None:
        return _ANNOUNCE_WAIT

    try:
        from astreum.communication.storage_request.model import max_batch_entries
        from astreum.utils.config import DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES

        if state is None:
            state = iter_records_in_cold_storage(node)
            node.startup_announce = state
        budget = node.config.get(
            "storage_put_batch_max_bytes", DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES
        )
        per_batch = max(1, max_batch_entries(budget))
        batch = list(islice(state, per_batch))
        if batch:
            put_exprs_in_network(node, [(expr_id, RESOLUTION_SINGLE) for expr_id in batch])
        if len(batch) < per_batch:
            node.startup_announce = _ANNOUNCE_DONE
            return _ANNOUNCE_DONE
    except Exception as exc:
        node.logger.exception("Startup announce failed: %s", exc)
        node.startup_announce = _ANNOUNCE_DONE
        return _ANNOUNCE_DONE
    return _ANNOUNCE_MORE


def _send_index_requests(node: "Node") -> bool:
    """Send one ``STORAGE_INDEX_REQUEST`` to every known peer, once.

    Waits until ``node.latest_block`` is loaded, so the replies can pass the
    storage trie check, and until at least one peer is known.

    Args:
        node: A Node instance.

    Returns:
        True if the requests were sent in this call, False otherwise.
    """
    if node.index_requested or getattr(node, "latest_block", None) is None:
        return False
    with node.peers_lock:
        peers = list(node.peers.values())
    peers = [peer for peer in peers if peer.address is not None]
    if not peers:
        return False
    node.index_requested = True
    request = StorageRequest(code=StorageRequestCode.STORAGE_INDEX_REQUEST)
    for peer in peers:
        _send_storage_put(node, peer, request)
    return True


def _send_branch_index_requests(node: "Node") -> bool:
    """Send a ``STORAGE_INDEX_REQUEST`` to the peers nearest to this node.

    The nodes that hold the entries of this branch are among the peers nearest
    to this node's own key. A timer walk calls this after it completes, so an
    entry that the network made after the join reaches this node. Waits for
    ``node.latest_block``, like ``_send_index_requests``.

    Args:
        node: A Node instance.

    Returns:
        True if a request was sent to at least one peer, False otherwise.
    """
    if getattr(node, "latest_block", None) is None:
        return False
    route = node.peer_route
    peers = route.closest_peers(node.storage_public_key_bytes, route.bucket_size)
    peers = [peer for peer in peers if peer.address is not None]
    request = StorageRequest(code=StorageRequestCode.STORAGE_INDEX_REQUEST)
    for peer in peers:
        _send_storage_put(node, peer, request)
    return bool(peers)


def _serve_one_index_request(node: "Node") -> bool:
    """Answer one queued ``STORAGE_INDEX_REQUEST``.

    Scans ``node.storage_index`` and sends the requester every entry for which
    fewer than ``PROVIDERS_PER_RECORD`` known nodes are nearer to the key than
    the requester. The known nodes are the route of this node, this node and
    the listed providers. The requester itself never counts. Entries go out as
    ``STORAGE_PUT`` datagrams, grouped by provider set. Each datagram carries
    the original provider payloads of the entries, so an entry with two
    providers goes out once. This node keeps its own entries.

    Args:
        node: A Node instance.

    Returns:
        True if a queued request was taken, False if the queue was empty.
    """
    with node.index_requests_lock:
        if not node.index_requests:
            return False
        requester_key = node.index_requests.pop(0)

    peer = get_peer(node, requester_key)
    if peer is None or peer.address is None:
        return True

    own_key = node.config["storage_public_key_bytes"]
    groups: dict[bytes, list[tuple[bytes, int]]] = {}
    for expr_id, provider_ids in list(node.storage_index.items()):
        payloads = [provider_payload_for_id(node, i) for i in provider_ids]
        provider_keys = []
        for payload in payloads:
            if payload is None:
                continue
            try:
                provider_keys.append(decode_storage_provider(payload)[0])
            except ValueError:
                continue
        place = node.peer_route.count_nearer(
            expr_id,
            requester_key,
            PROVIDERS_PER_RECORD,
            extra=[own_key, *provider_keys],
        )
        if place < PROVIDERS_PER_RECORD:
            payload = b"".join(p for p in payloads if p)
            if payload:
                groups.setdefault(payload, []).append((expr_id, RESOLUTION_SINGLE))

    for payload, entries in groups.items():
        send_put_entries(node, peer, payload, entries)
    return True


def _index_handoff_step(node: "Node", timer: SelfLookupTimer) -> bool:
    """Run one index handoff step: the walk, our request, then one reply.

    Our first request goes to every known peer when the first walk to our
    branch is complete. Each later walk that completes sends a request to the
    peers nearest to this node. A reply to another node does not wait for the
    walk.

    Args:
        node: A Node instance.
        timer: The state of the repeating self-lookup walk.

    Returns:
        True if more queued requests remain to answer, False otherwise.
    """
    completed_before = timer.completed
    if self_lookup_timer_step(node, timer, time.monotonic()):
        if not node.index_requested:
            _send_index_requests(node)
        elif timer.completed > completed_before:
            _send_branch_index_requests(node)
    _serve_one_index_request(node)
    with node.index_requests_lock:
        return bool(node.index_requests)


def advertise_storage(astreum_node: "Node") -> None:
    """Periodically update the storage request price and long-term-store.

    Runs a single timed loop in a daemon thread that adjusts
    ``storage_request_current_price`` based on incoming queue pressure to
    signal demand. When ``is_storage_provider`` is enabled, the same loop also
    announces every stored record once after sync, then processes one storage-index entry per ``long_term_storage_interval``
    seconds: entries missing from the records table are fetched and written
    to cold storage plus the records table. Fresh-expr advertisement happens
    at block creation via ``advertise_exprs(entries=...)``. The periodic
    re-advertise loop was removed.

    The thread exits when you set ``communication_stop_event``.

    Args:
        astreum_node: A Node instance with storage and communication infrastructure
            already initialized (``setup_storage`` must have been called).

    Returns:
        None. Runs indefinitely until ``communication_stop_event`` fires.
    """
    price_interval = float(astreum_node.config.get("storage_request_price_interval") or 0)
    prune_interval = float(astreum_node.config.get("expr_request_ttl") or 0)

    long_term_interval = float(
        getattr(astreum_node, "long_term_storage_interval", 0) or 0
    )
    long_term_enabled = bool(getattr(astreum_node, "is_storage_provider", False))

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
    next_prune_at = now + prune_interval if prune_interval > 0 else None
    next_announce_at = now if getattr(astreum_node, "is_storage_provider", False) else None
    next_index_at = now
    timer = SelfLookupTimer()

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

        if next_announce_at is not None and now >= next_announce_at:
            progress = _startup_announce_step(astreum_node)
            if progress == _ANNOUNCE_DONE:
                next_announce_at = None
            elif progress == _ANNOUNCE_MORE:
                next_announce_at = now
            else:
                next_announce_at = now + 1.0

        if now >= next_index_at:
            try:
                more = _index_handoff_step(astreum_node, timer)
            except Exception as exc:
                astreum_node.logger.exception("Index handoff step failed: %s", exc)
                more = False
            next_index_at = now if more else now + 1.0

        if next_prune_at is not None and now >= next_prune_at:
            try:
                removed = prune_expired_expr_reqs(astreum_node)
                if removed:
                    astreum_node.logger.debug("Pruned %d expired expr requests", removed)
            except Exception as exc:
                astreum_node.logger.exception("Expr request prune failed: %s", exc)
            while next_prune_at <= now:
                next_prune_at += prune_interval

        deadlines = [
            d
            for d in (
                next_price_at,
                next_long_term_at,
                next_prune_at,
                next_announce_at,
                next_index_at,
            )
            if d is not None
        ]
        if not deadlines:
            stop.wait(1.0)
            continue
        wait_timeout = max(0.0, min(deadlines) - now)
        if stop.wait(wait_timeout):
            break

    astreum_node.logger.info("Storage advertiser stopped")
