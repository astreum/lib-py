"""Consensus fork verification helpers."""

from __future__ import annotations

from typing import Any

from astreum.consensus.block.verify import verify_block_head, verify_block_transactions
from astreum.expression import ZERO32
from astreum.consensus.block.encoding.decode import get_block_from_storage
from astreum.consensus.block.holders import block_holder
from astreum.consensus.fork.node import update_fork


def verify_fork(node: Any, head_id: bytes) -> bool:
    """Verify fork headers from head to root.

    Args:
        node: The node to verify on.
        head_id: The fork head identifier.

    Returns:
        True if verification succeeds, False otherwise.
    """
    with node.forks_lock:
        fork = node.forks[head_id]
        root = fork.root
    current_hash = head_id
    while True:
        block_contact = block_holder(node, current_hash)
        block = get_block_from_storage(
            astreum_node=node, block_hash=current_hash, contact=block_contact
        )

        previous_block = None
        if block.previous_block_hash != ZERO32:
            previous_block = get_block_from_storage(
                astreum_node=node,
                block_hash=block.previous_block_hash,
                contact=block_holder(node, block.previous_block_hash) or block_contact,
            )
        block.previous_block = previous_block

        if not verify_block_head(node, block):
            update_fork(node, head_id, {"malicious_block_hash": current_hash})
            return False

        with node.forks_lock:
            if current_hash in node.forks and current_hash != head_id:
                fork.header_verified_up_to = current_hash
                fork.root = current_hash
                root = current_hash
                break

        if current_hash == root:
            update_fork(node, head_id, {"header_verified_up_to": root})
            break

        if block.previous_block_hash == ZERO32:
            update_fork(node, head_id, {"header_verified_up_to": ZERO32, "root": ZERO32})
            root = ZERO32
            break

        current_hash = block.previous_block_hash

    current_hash = head_id
    while True:
        block_contact = block_holder(node, current_hash)
        block = get_block_from_storage(
            astreum_node=node, block_hash=current_hash, contact=block_contact
        )
        previous_block = None
        if block.previous_block_hash != ZERO32:
            previous_block = get_block_from_storage(
                astreum_node=node,
                block_hash=block.previous_block_hash,
                contact=block_holder(node, block.previous_block_hash) or block_contact,
            )
        block.previous_block = previous_block
        validator_bytes = node.config.validation_public_key_bytes
        if validator_bytes and block.validator_public_key_bytes == validator_bytes:
            update_fork(node, head_id, {"transactions_verified_up_to": current_hash})
        else:
            verified, reason = verify_block_transactions(node, block)
            if verified:
                update_fork(node, head_id, {"transactions_verified_up_to": current_hash})
            else:
                block_id = (
                    block.expr_id.hex()
                    if isinstance(block.expr_id, (bytes, bytearray))
                    else str(current_hash)
                )
                node.logger.warning(
                    "Block verification failed block=%s reason=%s",
                    block_id,
                    reason,
                )
                update_fork(node, head_id, {"malicious_block_hash": current_hash})
                return False
        if current_hash == root or block.previous_block_hash == ZERO32:
            return True
        current_hash = block.previous_block_hash
