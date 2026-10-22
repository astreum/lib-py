"""Consensus fork verification worker."""

from __future__ import annotations

import time
from typing import Any, Set

from astreum.consensus.fork.model import Fork
from astreum.consensus.fork.verify import verify_fork
from astreum.consensus.block.encoding.decode import get_block_from_storage
from astreum.consensus.block.holders import block_holder


def _is_fully_verified(fork: Any) -> bool:
    """Check if a fork is fully verified.

    Args:
        fork: The fork to check.

    Returns:
        True if fully verified, else False.
    """
    return (
        fork.header_verified_up_to == fork.root
        and fork.transactions_verified_up_to == fork.root
    )


def _merge_forks(node: Any) -> bool:
    """Merge forks that share a root.

    Args:
        node: The node.

    Returns:
        True if any forks were merged.
    """
    merged = False
    with node.forks_lock:
        forks = node.forks
        merge_heads = [
            head
            for head, fork in forks.items()
            if fork.root in forks and _is_fully_verified(fork)
        ]
        root_counts: dict[bytes, int] = {}
        for head in merge_heads:
            root = forks[head].root
            root_counts[root] = root_counts.get(root, 0) + 1
        merge_heads = [
            head for head in merge_heads if root_counts[forks[head].root] == 1
        ]
        merge_heads = [
            head
            for head in merge_heads
            if _is_fully_verified(forks[forks[head].root])
        ]

        for head in merge_heads:
            child_fork = forks.get(head)
            if child_fork is None:
                continue
            root_head = child_fork.root
            root_fork = forks.get(root_head)
            if root_fork is None:
                continue
            unified_root = root_fork.root
            unified_fork = Fork(
                head=head,
                root=unified_root,
                header_verified_up_to=unified_root,
                transactions_verified_up_to=unified_root,
            )
            unified_fork.peers.update(root_fork.peers)
            unified_fork.peers.update(child_fork.peers)
            del forks[root_head]
            del forks[head]
            forks[head] = unified_fork
            merged = True
    return merged


def _scan_peer_heads(node: Any) -> None:
    """Scan peers for latest block hashes and update forks/peers.

    Args:
        node: The node to scan.

    Returns: None
    """
    grouped: dict[bytes, set[Any]] = {}
    with node.peers_lock:
        peers = list(node.peers.items())
    for peer_id, peer in peers:
        latest = peer.latest_block
        if latest is None:
            continue
        peer_set = grouped.get(latest)
        if peer_set is None:
            peer_set = set()
            grouped[latest] = peer_set
        peer_set.add(peer_id)

    with node.forks_lock:
        for fork in node.forks.values():
            fork.peers = set()
        for head_id, peer_ids in grouped.items():
            fork = node.forks.get(head_id)
            if fork is None:
                fork = Fork(head=head_id)
                node.forks[head_id] = fork
            fork.peers = set(peer_ids)


def _select_latest_block(node: Any) -> None:
    """Select the latest verified block as the chain head.

    Args:
        node: The node.

    Returns: None
    """
    max_stale = node.config.verification_max_stale_seconds
    max_future = node.config.verification_max_future_skew

    now = int(time.time())
    with node.latest_block_lock:
        current_head = node.latest_block_hash

    best_head = None
    best_block = None
    best_height = -1
    with node.forks_lock:
        forks = list(node.forks.items())
    for head, fork in forks:
        if fork.malicious_block_hash is not None:
            continue
        if not _is_fully_verified(fork):
            continue
        if not getattr(fork, "peers", None):
            continue
        if not isinstance(head, (bytes, bytearray)):
            continue
        try:
            block = get_block_from_storage(
                astreum_node=node, block_hash=head, contact=block_holder(node, head)
            )
        except Exception:
            continue
        ts = getattr(block, "timestamp", None)
        if ts is None:
            continue
        ts_int = ts
        if max_stale >= 0 and (now - ts_int) > max_stale:
            continue
        if max_future >= 0 and (ts_int - now) > max_future:
            continue
        height = block.height
        if height > best_height:
            best_head = head
            best_block = block
            best_height = height
            continue
        if height == best_height:
            if current_head == head:
                best_head = head
                best_block = block
                best_height = height
            elif current_head != best_head and best_head is not None:
                if head < best_head:
                    best_head = head
                    best_block = block
                    best_height = height

    if best_head is None or best_block is None:
        return
    with node.latest_block_lock:
        if node.latest_block_hash != best_head:
            node.latest_block_hash = best_head
            node.latest_block = best_block
            node.block_spacing = 2


def make_verify_worker(node: Any):
    """Build the consensus fork verify worker bound to the given node.

    Args:
        node: The node.

    Returns:
        The worker function.
    """
    def _verify_worker() -> None:
        """The verification worker loop.

        Args:
            None.

        Returns: None
        """
        stop = node._verify_stop_event
        while not stop.is_set():
            _scan_peer_heads(node)

            with node.forks_lock:
                heads = list(node.forks.keys())

            if not heads:
                time.sleep(0.1)
                continue

            for head_id in heads:
                try:
                    verify_fork(node, head_id)
                except Exception:
                    node.logger.exception("Fork verify failed head=%s", head_id)

            while _merge_forks(node):
                continue

            if not node.config.default_seed:
                _select_latest_block(node)
            time.sleep(node.config.verify_blockchain_interval)

    return _verify_worker
