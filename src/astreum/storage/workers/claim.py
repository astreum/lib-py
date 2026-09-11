from __future__ import annotations

import time
from typing import TYPE_CHECKING

from blake3 import blake3

from astreum.consensus.block.rate import calculate_storage_fee
from astreum.consensus.transaction import create_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.storage.model import StorageRecord
from astreum.consensus.transaction.storage.payment import _leading_zero_bits, _required_bits
from astreum.crypto.bloom_search import ERA_SIZE
from astreum.expression import Expr, NIL, int_, bytes_, link, ZERO32
from astreum.expression.encoding import encode_expr_to_bytes
from astreum.expression.expr import _encode_int
from astreum.storage.exprs import get_expr_from_local_storage
from astreum.storage.records import iter_records_in_cold_storage, get_record_from_cold_storage
from astreum.storage.radix import get_from_radix_tree

if TYPE_CHECKING:
    from astreum.node import Node


def _compute_pow_and_challenge(
    node: Node,
    storage_id: bytes,
    record: StorageRecord,
) -> tuple[bytes, bytes, int] | None:
    """Compute a PoW claim for the given record.

    The challenged slot's data id is resolved from the record's value blob in
    the records table (byte offset = sequence).  Returns
    (storage_id, slot_id, nonce) or None if unavailable.
    """
    last_payment_block_hash = record.last_payment_block_hash
    if len(last_payment_block_hash) != 32:
        return None
    if record.new_count <= 0:
        return None

    # Determine which slot to challenge
    challenge_seed = blake3(last_payment_block_hash + storage_id).digest()
    challenge_index = (
        int.from_bytes(challenge_seed[:8], "little", signed=False) % record.new_count
    )

    # Resolve the challenged slot from the records table value blob
    value = get_record_from_cold_storage(node, storage_id)
    if value is None:
        return None
    offset = challenge_index * 32
    if offset + 32 > len(value):
        return None
    slot_id = value[offset : offset + 32]
    if slot_id == ZERO32 or len(slot_id) != 32:
        return None

    # Fetch the actual data
    data_expr = get_expr_from_local_storage(node, slot_id)
    if data_expr is None:
        return None

    fetched_data_bytes = encode_expr_to_bytes(data_expr)
    sender_bytes = node.storage_public_key_bytes

    # Compute required bits
    required_bits = _required_bits(
        sender=sender_bytes,
        last_payment_winner=record.last_payment_winner,
        block_height=node.latest_block.height,
        last_payment_height=record.last_payment_height,
        base_bits=0,
    )

    # Brute-force PoW
    nonce = 0
    max_nonce = 1 << 30
    while nonce < max_nonce:
        nonce_encoded = _encode_int(nonce)
        work_hash = blake3(
            last_payment_block_hash
            + sender_bytes
            + storage_id
            + slot_id
            + fetched_data_bytes
            + nonce_encoded
        ).digest()
        if _leading_zero_bits(work_hash) >= required_bits:
            return storage_id, slot_id, nonce
        nonce += 1

    return None


def _next_claim_counter(node: Node) -> int:
    """Resolve the counter for the next claim tx.

    Uses the greater of the on-chain sender-account counter and the
    node-local ``next_claim_counter`` (bumped after each successful send so
    consecutive eras don't wait for on-chain confirmation). A missing
    account, no latest block, or a fetch failure all mean 0.
    """
    if not hasattr(node, "next_claim_counter"):
        node.next_claim_counter = 0

    latest_block = getattr(node, "latest_block", None)
    on_chain = 0
    if latest_block is not None:
        try:
            account = latest_block.accounts.get_account(
                address=node.storage_public_key_bytes, node=node
            )
            if account is not None:
                on_chain = account.counter
        except Exception:
            pass
    return max(node.next_claim_counter, on_chain)


def _build_multi_claim_tx(
    node: Node,
    claims: list[tuple[bytes, bytes, int]],
    secret_key,
    counter: int,
) -> object:
    """Build a signed STORAGE_PAYMENT transaction with the given claims."""
    claims_expr = NIL
    for storage_id, slot_id, nonce in reversed(claims):
        claim = link(
            bytes_(storage_id),
            link(
                bytes_(slot_id),
                link(int_(nonce), NIL),
            ),
        )
        claims_expr = link(claim, claims_expr)

    return create_transaction(
        chain_id=node.config["chain_id"],
        sender=node.storage_public_key_bytes,
        counter=counter,
        recipient=b"\x00" * 32,
        code=TransactionCode.STORAGE_PAYMENT,
        amount=0,
        data=claims_expr,
        secret_key=secret_key,
    )


def _build_claims_for_records(
    node: Node,
    latest_block,
    spacing_eras: dict[bytes, int],
) -> list[tuple[bytes, bytes, int, int]]:
    """Evaluate every record in the records table and build eligible claims.

    Returns ``(storage_id, slot_id, nonce, payout)`` tuples,
    where ``payout = new_size × (block_height − last_payment_height)``
    mirrors the reward calculation in ``storage/payment.py`` so the submit
    gate can check the bundle's aggregate economics.
    """
    from astreum.consensus.constants import STORAGE_ADDRESS

    claims_to_make: list[tuple[bytes, bytes, int, int]] = []
    seen: set[bytes] = set()

    storage_account = None
    if hasattr(latest_block, "accounts"):
        storage_account = latest_block.accounts.get_account(STORAGE_ADDRESS, node)

    for storage_id in iter_records_in_cold_storage(node):
        if storage_id in seen:
            continue
        seen.add(storage_id)

        contract_head = None
        try:
            if storage_account is not None:
                contract_head = get_from_radix_tree(storage_account.data, node, storage_id)
        except Exception:
            continue
        if not contract_head:
            continue

        record = StorageRecord.from_storage(node, contract_head.hash())
        if record is None:
            continue

        current_spacing = spacing_eras.get(storage_id, 1)
        eras_elapsed = (latest_block.height - record.last_payment_height) // ERA_SIZE
        payout = record.new_size * (latest_block.height - record.last_payment_height)

        if record.last_payment_winner == node.storage_public_key_bytes:
            # We're incumbent
            if eras_elapsed >= current_spacing:
                claim = _compute_pow_and_challenge(node, storage_id, record)
                if claim is not None:
                    claims_to_make.append((*claim, payout))
                    spacing_eras[storage_id] = min(current_spacing + 1, 4)
        else:
            # Someone else is incumbent
            spacing_eras[storage_id] = 1  # reset
            if eras_elapsed >= 5:  # wall dropping (fib(8)=21, ~2Mx harder)
                claim = _compute_pow_and_challenge(node, storage_id, record)
                if claim is not None:
                    claims_to_make.append((*claim, payout))
    return claims_to_make


_BASE_TX_FEE = 1  # apply.py:77 — protocol-derived base transaction fee
_RECEIPT_OVERHEAD_BYTES = 160  # fixed-size 8-field receipt expr (models/receipt.py:42-58)


def _submit_claims(node: Node, entries: list[tuple[bytes, bytes, int, int]]) -> None:
    """Bundle the given claims into a single STORAGE_PAYMENT tx and send it,
    only if the aggregate payout covers the tx's protocol cost."""
    if not entries:
        return
    latest_block = getattr(node, "latest_block", None)
    if latest_block is None:
        return
    try:
        claims = [entry[:3] for entry in entries]
        counter = _next_claim_counter(node)
        tx = _build_multi_claim_tx(node, claims, node.storage_secret_key, counter)
        try:
            cost = (
                _BASE_TX_FEE
                + calculate_storage_fee(latest_block, len(encode_expr_to_bytes(tx.expr())))
                + calculate_storage_fee(latest_block, _RECEIPT_OVERHEAD_BYTES)
            )
        except ValueError:
            # cumulative_total_fee <= 0 (young/odd block, rate.py:36-37)
            return  # pass skipped; retried next era
        total_payout = sum(entry[3] for entry in entries)
        if total_payout < cost:
            node.logger.info(
                "Claim worker: bundle unprofitable (%d payouts < %d cost); dropped",
                total_payout,
                cost,
            )
            return  # drop, retry next era
        from astreum.consensus.transaction.send import send_transaction
        send_transaction(node, tx)
        node.next_claim_counter = counter + 1
        node.logger.info(
            "Claim worker: submitted %d claim(s) in tx %s (payout %d, cost %d)",
            len(claims),
            tx.hash.hex() if tx.hash else "unknown",
            total_payout,
            cost,
        )
    except Exception as exc:
        node.logger.exception("Claim worker: failed to submit claims: %s", exc)


def claim_storage(node: Node) -> None:
    """Submit storage reward claims on era boundaries.

    Runs as a daemon thread.  On each era boundary (every ``ERA_SIZE``
    blocks) it iterates the records table on disk, and for each record where
    this node is a provider, computes a proof-of-work challenge for the
    challenged slot and submits a single ``STORAGE_PAYMENT`` transaction
    bundling all claims.

    The claim set is the records table itself — written incrementally by the
    validator — so nothing is rebuilt at boot and there is no cold-expr scan.

    Claim spacing uses a progressive back-off when incumbent
    (``claim_spacing_eras``, 1→4 eras) and a wall-drop threshold of
    5 eras for non-incumbent takeover attempts.

    Args:
        node: A fully initialized Node instance with
            ``storage_public_key_bytes``, ``storage_secret_key``, and a
            connected P2P communication layer.

    Returns:
        None.  Runs indefinitely until ``communication_stop_event``
        is set.
    """
    stop = node.communication_stop_event
    last_checked_era = -1

    if not hasattr(node, "claim_spacing_eras"):
        node.claim_spacing_eras = {}

    while not stop.is_set():
        latest_block = getattr(node, "latest_block", None)
        if latest_block is None:
            stop.wait(1.0)
            continue

        current_era = latest_block.height // ERA_SIZE
        if current_era == last_checked_era:
            # Wait until next era boundary
            next_boundary = (current_era + 1) * ERA_SIZE
            blocks_remaining = next_boundary - latest_block.height
            wait_seconds = max(1.0, blocks_remaining * 0.5)
            stop.wait(wait_seconds)
            continue

        # Era changed — evaluate all records in the records table
        claims_to_make = _build_claims_for_records(
            node,
            latest_block,
            node.claim_spacing_eras,
        )
        _submit_claims(node, claims_to_make)

        last_checked_era = current_era
        stop.wait(0.5)
