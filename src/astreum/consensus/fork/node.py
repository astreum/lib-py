"""Fork storage helpers."""

from __future__ import annotations

from threading import Lock
from typing import Any, Mapping

from astreum.consensus.fork.model import Fork
from astreum.expression import ZERO32


def fork_setup(node: Any) -> None:
    """Initialize fork storage and lock on the node."""

    Args:
        node (Any): Description.

    if hasattr(node, 'forks'):
        return
    node.forks = {}
    node.forks_lock = Lock()


def update_fork(node: Any, head_id: bytes, updates: Mapping[str, Any]) -> None:
    """Update fields on an existing fork by head_id."""

    Args:
        node (Any): Description.
        head_id (bytes): Description.
        updates (Mapping[str, Any]): Description.

    with node.forks_lock:
        fork = node.forks.get(head_id)
        if fork is None:
            raise KeyError("fork head not found")
        for key, value in updates.items():
            setattr(fork, key, value)


def import_forks(node: Any, payload: bytes) -> dict[bytes, Fork]:
    """Load forks from a byte stream into node.forks."""

    Args:
        node (Any): Description.
        payload (bytes): Description.

    # Fork encoding size: head + root + header flag + header + tx flag + tx + malicious flag + malicious
    block_size = len(ZERO32)
    fork_size = (
        block_size
        + block_size
        + 1
        + block_size
        + 1
        + block_size
        + 1
        + block_size
    )
    if len(payload) % fork_size != 0:
        raise ValueError("fork payload length mismatch")
    with node.forks_lock:
        for offset in range(0, len(payload), fork_size):
            fork = Fork.from_bytes(payload[offset : offset + fork_size])
            node.forks[fork.head] = fork
    return node.forks


def export_forks(node: Any) -> bytes:
    """Export node.forks as a single byte stream."""

    Args:
        node (Any): Description.

    with node.forks_lock:
        return b"".join(fork.to_bytes() for fork in node.forks.values())
