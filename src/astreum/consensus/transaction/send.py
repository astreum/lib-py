from __future__ import annotations

import time
from typing import TYPE_CHECKING

from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer import get_peer
from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.expression import resolve_inner_exprs
from astreum.storage.exprs import put_expr_in_hot_storage
from astreum.storage.cold import put_expr_in_cold_storage

if TYPE_CHECKING:
    from astreum.consensus.transaction.model import Transaction


def send_transaction(
    node: "Node",
    transaction: "Transaction",
) -> bytes:
    """Atomize, store, advertise, and broadcast an already-signed Transaction.

    The caller must sign the transaction before calling this function:

        tx.sign(sender_key)
        send_transaction(node, tx)

    Returns the transaction's content-addressed hash (expr_id).
    """
    if not node.is_connected:
        raise RuntimeError("node not connected")

    latest_block = node.latest_block
    if latest_block is None:
        raise RuntimeError("latest block unavailable")

    tx_hash = transaction.expr().hash()
    tx_exprs, missed = resolve_inner_exprs(node, transaction.expr())
    if missed:
        node.logger.warning(
            "Transaction resolution failed for tx %s: missed=%s",
            tx_hash.hex(),
            [m.hex()[:8] for m in missed],
        )
        raise RuntimeError("transaction data unavailable locally; cannot broadcast whole message")
    hot_store_failures = 0

    for tx_expr in tx_exprs:
        if not put_expr_in_hot_storage(node, tx_expr):
            hot_store_failures += 1
        put_expr_in_cold_storage(node, tx_expr)

    if hot_store_failures:
        node.logger.warning(
            "Transaction hot storage writes skipped for tx %s: count=%s",
            tx_hash.hex(),
            hot_store_failures,
        )

    from astreum.consensus.transaction.message import encode_transaction_message
    from astreum.communication.message_pow import MAX_INLINE_MESSAGE_BYTES
    payload = encode_transaction_message(tx_exprs)
    if len(payload) > MAX_INLINE_MESSAGE_BYTES:
        node.logger.warning(
            "Transaction payload exceeds %s bytes for tx %s (got %s); cannot be committed yet",
            MAX_INLINE_MESSAGE_BYTES,
            tx_hash.hex(),
            len(payload),
        )
        raise RuntimeError("transaction payload too large for inline message")

    validation_route = node.validation_route
    if validation_route is None:
        raise RuntimeError("no validator available")

    has_validators = bool(getattr(validation_route, "peers", None))
    if not has_validators:
        with node.peers_lock:
            peers = list(node.peers.items())

        for _peer_key, peer in peers:
            if not getattr(peer, "address", None):
                continue
            route_request = Message(
                topic=MessageTopic.ROUTE_REQUEST,
                content=b"\x01",
                sender_public_key_bytes=node.storage_public_key_bytes,
            )
            route_request.encrypt(peer.shared_key_bytes)
            enqueue_outgoing(
                node,
                peer.address,
                message=route_request,
                difficulty=peer.difficulty,
            )

        wait_deadline = time.time() + float(node.config.peer_timeout_interval)
        while time.time() < wait_deadline:
            if getattr(validation_route, "peers", None):
                has_validators = True
                break
            time.sleep(0.1)

    if not has_validators:
        raise RuntimeError("no validator available")

    validators = {}
    for peer in validation_route.peers.values():
        if peer is not None and getattr(peer, "address", None):
            validators[getattr(peer, "public_key_bytes", None)] = peer
    for bucket in validation_route.buckets.values():
        for peer_key in bucket:
            peer = get_peer(node, peer_key)
            if peer is not None and getattr(peer, "address", None):
                validators[peer.public_key_bytes] = peer

    if not validators:
        raise RuntimeError("no validator available")

    for peer in validators.values():
        tx_message = Message(
            topic=MessageTopic.TRANSACTION,
            content=payload,
            sender_public_key_bytes=node.storage_public_key_bytes,
        )
        tx_message.encrypt(peer.shared_key_bytes)
        enqueue_outgoing(
            node,
            peer.address,
            message=tx_message,
            difficulty=peer.difficulty,
        )

    return tx_hash
