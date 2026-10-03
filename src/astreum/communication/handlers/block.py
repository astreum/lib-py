from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING

from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer import Peer
from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.consensus.block.encoding.decode import get_block_from_storage
from astreum.consensus.block.holders import has_block_holder, record_block_holder
from astreum.consensus.block.message import decode_block_message
from astreum.consensus.block.verify.head import verify_block_head
from astreum.consensus.validation.peers import validator_peers
from astreum.storage.exprs import put_expr_in_hot_storage

if TYPE_CHECKING:
    from astreum import Node


def _scratch_node(node: "Node", limit_bytes: int = 4 * 1024 * 1024) -> SimpleNamespace:
    """A throwaway in-memory store to decode an unverified header in.

    Lets ``get_block_from_storage`` read the header without touching this
    node's storage or the network until the header has passed its checks.
    """
    return SimpleNamespace(
        hot_storage={},
        hot_storage_timestamps={},
        hot_storage_lock=threading.RLock(),
        hot_storage_size=0,
        config={"hot_storage_limit": limit_bytes, "cold_storage_path": None},
        is_connected=False,
        logger=node.logger,
    )


def handle_block(node: "Node", peer: Peer, content: bytes) -> None:
    """Handle a validator's ``BLOCK`` announcement.

    Checks the header locally (no state reads, no network), then records the
    announced contact as the holder of the block's uncommitted exprs. A
    validator also adopts the block as its head and relays the announcement to
    the other validators, since any of them may be the next one.
    """
    decoded = decode_block_message(content)
    if decoded is None:
        node.logger.debug("BLOCK dropped from %s: invalid payload", peer.address)
        return
    contact, exprs = decoded
    block_hash = exprs[0].hash()

    if has_block_holder(node, block_hash):
        return

    with node.latest_block_lock:
        latest_hash = node.latest_block_hash
        latest_block = node.latest_block

    # Decode the header in a scratch store; nothing of this node's is touched.
    scratch = _scratch_node(node)
    for expr in exprs:
        put_expr_in_hot_storage(scratch, expr)
    try:
        block = get_block_from_storage(
            scratch, block_hash, contact=contact, load_bloom=False
        )
    except Exception as exc:
        node.logger.debug("BLOCK dropped from %s: undecodable header: %s", peer.address, exc)
        return

    if latest_block is None or latest_hash is None or block.previous_block_hash != latest_hash:
        node.logger.debug("BLOCK %s dropped: not built on our latest block", block_hash.hex())
        return

    block.previous_block = latest_block
    if not verify_block_head(node, block):
        node.logger.debug("BLOCK %s dropped: header failed verification", block_hash.hex())
        return

    for expr in exprs:
        put_expr_in_hot_storage(node, expr)
    if not record_block_holder(node, block_hash, contact):
        return

    is_validator = node.config.get("validation_secret_key") is not None
    if not is_validator:
        return

    with node.latest_block_lock:
        if node.latest_block_hash == latest_hash:
            node.latest_block_hash = block_hash
            node.latest_block = block
    node.logger.info("Adopted block #%s %s announced by a validator", block.height, block_hash.hex())

    _relay(node, peer, content)


def _relay(node: "Node", sender: Peer, content: bytes) -> None:
    """Forward the announcement once to every other validator."""
    for key, validator in validator_peers(node).items():
        if key == sender.public_key_bytes:
            continue
        try:
            message = Message(
                topic=MessageTopic.BLOCK,
                content=content,
                sender_public_key_bytes=node.storage_public_key_bytes,
            )
            message.encrypt(validator.shared_key_bytes)
            enqueue_outgoing(
                node,
                validator.address,
                message=message,
                difficulty=validator.difficulty,
            )
        except Exception as exc:
            node.logger.debug("BLOCK relay to %s failed: %s", validator.address, exc)
