from __future__ import annotations

from typing import Any

from blake3 import blake3

from astreum.expression import Expr, resolve_inner_exprs, resolve_list_exprs, int_, bytes_, link
from astreum.expression.expr import _encode_int
from astreum.expression.encoding import encode_expr_to_bytes
from astreum.expression import ZERO32, NIL
from astreum.storage.radix import get_from_radix_tree, put_in_radix_tree
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.storage.model import StorageRecord, StorageSlot
from astreum.crypto.bloom_search import ERA_SIZE

# Fib(13) = 233, largest fib <= 256-bit hash size
_N = 13


def _fib(n: int) -> int:
    """Return the nth Fibonacci number (fib(0)=0, fib(1)=1, fib(2)=1, ...)."""
    if n < 0:
        return 0
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def _leading_zero_bits(buf: bytes) -> int:
    zeros = 0
    for byte in buf:
        if byte == 0:
            zeros += 8
            continue
        zeros += 8 - byte.bit_length()
        break
    return zeros


def _required_bits(*, sender: bytes, last_payment_winner: bytes,
                   block_height: int, last_payment_height: int,
                   base_bits: int) -> int:
    if sender == last_payment_winner:
        return base_bits
    eras_elapsed = (block_height - last_payment_height) // ERA_SIZE
    penalty = _fib(_N - min(eras_elapsed, _N))
    return base_bits + penalty


def _parse_claim(claim_expr: Expr) -> tuple[bytes, bytes, int] | None:
    """Parse a single claim from its Expr representation.

    Claim format: Link(Bytes(storage_id), Link(Bytes(slot_id), Link(Int(nonce), NIL)))
    """
    if not claim_expr._tag == "link":
        return None
    head = claim_expr._head
    tail = claim_expr._tail
    if not head._tag == "bytes" or not tail._tag == "link":
        return None
    storage_id = head.value
    head2 = tail._head
    tail2 = tail._tail
    if not head2._tag == "bytes" or not tail2._tag == "link":
        return None
    slot_id = head2.value
    head3 = tail2._head
    tail3 = tail2._tail
    if not head3._tag == "int" or tail3 is not NIL:
        return None
    nonce = head3.value
    return storage_id, slot_id, nonce


def _verify_single_claim(
    node: Any,
    block: object,
    transaction: Transaction,
    storage_account: Any,
    claim: tuple[bytes, bytes, int],
) -> bool:
    """Verify and pay out a single storage claim. Returns True on success."""
    storage_id, slot_id, nonce = claim

    # 1. Fetch StorageRecord from storage trie
    contract_head = get_from_radix_tree(storage_account.data, node, storage_id)
    if not contract_head or contract_head == ZERO32:
        return False
    record = StorageRecord.from_storage(node, contract_head.hash())
    if record is None:
        return False

    last_payment_block_hash = record.last_payment_block_hash
    if len(last_payment_block_hash) != 32:
        return False

    # 2. Derive challenge index
    challenge_seed = blake3(last_payment_block_hash + storage_id).digest()
    challenge_index = (
        int.from_bytes(challenge_seed[:8], "little", signed=False) % record.new_count
    )

    # 3. Fetch StorageSlot from storage trie, verify it belongs to this record
    slot = StorageSlot.from_storage(node, slot_id)
    if slot is None:
        return False
    if slot.storage_id != storage_id:
        return False
    if slot.sequence != challenge_index:
        return False

    # 4. Fetch data via STORAGE_GET from network
    from astreum.storage.exprs import get_expr_from_local_storage
    data_expr = get_expr_from_local_storage(node, slot_id)
    if data_expr is None:
        return False

    fetched_data_bytes = encode_expr_to_bytes(data_expr)

    # 5. Compute work hash
    nonce_encoded = _encode_int(nonce)
    sender_bytes = transaction.sender
    work_hash = blake3(
        last_payment_block_hash
        + sender_bytes
        + storage_id
        + slot_id
        + fetched_data_bytes
        + nonce_encoded
    ).digest()

    base_bits = 0  # storage payment uses pure PoW; no XOR-distance base
    required_bits = _required_bits(
        sender=sender_bytes,
        last_payment_winner=record.last_payment_winner,
        block_height=block.height,
        last_payment_height=record.last_payment_height,
        base_bits=base_bits,
    )

    if _leading_zero_bits(work_hash) < required_bits:
        return False

    # 6. Payout
    total_bytes = record.new_size
    if total_bytes <= 0:
        return False
    if block.height <= record.last_payment_height:
        return False
    payout = total_bytes * (block.height - record.last_payment_height)
    if payout <= 0:
        return False

    # Must update outside the per-claim loop (partial success).
    # We mutate storage_account on the last successful claim.
    # For now, apply inline and track updated records at the caller level.
    return True


def handle_storage_payment_contract(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
    sender_account: Any,
    storage_account: Any,
    payload: Expr,
) -> tuple[bool, int]:
    """Handle a storage-payment contract transaction.

    ``payload`` is the transaction's ``data`` field, which is an Expr
    representing a link list of claims.

    Multi-claim: each claim is verified independently.  Invalid claims are
    silently skipped.  The transaction succeeds if at least one claim is valid.
    The record is updated only for the last valid claim (all share the same
    ``storage_id`` per the plan — or we could support multiple records
    by tracking the last update per record key).

    Returns (any_valid, total_minted) where total_minted is the sum of payouts
    for claims where record.mint is True.
    """
    try:
        # Resolve the link list of claims
        claims_nodes, missed = resolve_list_exprs(node, payload)
        if missed:
            return (False, 0)

        # Parse each claim
        parsed_claims: list[tuple[bytes, bytes, int]] = []
        for claim_node in claims_nodes:
            parsed = _parse_claim(claim_node)
            if parsed is not None:
                parsed_claims.append(parsed)

        if not parsed_claims:
            return (False, 0)

        # Verify each claim; collect the last valid record update data
        last_valid_record: StorageRecord | None = None
        last_valid_storage_id: bytes | None = None
        any_valid = False
        minted_sum = 0

        for storage_id, slot_id, nonce in parsed_claims:
            # Fetch StorageRecord
            contract_head = get_from_radix_tree(storage_account.data, node, storage_id)
            if not contract_head or contract_head == ZERO32:
                continue
            record = StorageRecord.from_storage(node, contract_head.hash())
            if record is None:
                continue

            last_payment_block_hash = record.last_payment_block_hash
            if len(last_payment_block_hash) != 32:
                continue

            # Derive challenge index
            challenge_seed = blake3(last_payment_block_hash + storage_id).digest()
            challenge_index = (
                int.from_bytes(challenge_seed[:8], "little", signed=False) % record.new_count
            )

            # Fetch StorageSlot, verify it belongs to this record
            slot = StorageSlot.from_storage(node, slot_id)
            if slot is None:
                continue
            if slot.storage_id != storage_id:
                continue
            if slot.sequence != challenge_index:
                continue

            # Fetch data from network
            from astreum.storage.exprs import get_expr_from_local_storage
            data_expr = get_expr_from_local_storage(node, slot_id)
            if data_expr is None:
                continue

            fetched_data_bytes = encode_expr_to_bytes(data_expr)
            nonce_encoded = _encode_int(nonce)
            sender_bytes = transaction.sender
            work_hash = blake3(
                last_payment_block_hash
                + sender_bytes
                + storage_id
                + slot_id
                + fetched_data_bytes
                + nonce_encoded
            ).digest()

            required_bits = _required_bits(
                sender=sender_bytes,
                last_payment_winner=record.last_payment_winner,
                block_height=block.height,
                last_payment_height=record.last_payment_height,
                base_bits=0,
            )
            if _leading_zero_bits(work_hash) < required_bits:
                continue

            total_bytes = record.new_size
            if total_bytes <= 0:
                continue
            if block.height <= record.last_payment_height:
                continue
            payout = total_bytes * (block.height - record.last_payment_height)
            if payout <= 0:
                continue

            sender_account.balance += payout
            minted_sum += payout if record.mint else 0

            last_valid_record = StorageRecord(
                creation_block_hash=record.creation_block_hash,
                last_payment_block_hash=block.previous_block_hash,
                last_payment_height=block.height,
                last_payment_winner=sender_bytes,
                new_size=record.new_size,
                new_count=record.new_count,
            )
            last_valid_storage_id = storage_id
            any_valid = True

        if not any_valid:
            return (False, 0)

        # Update the storage trie for the last valid record
        updated_record_head = last_valid_record.expr().hash()
        put_in_radix_tree(storage_account.data, node, last_valid_storage_id, updated_record_head)
        storage_account.data_hash = storage_account.data.root_hash

        inner_exprs, _ = resolve_inner_exprs(node, last_valid_record.expr())
        block.pending_exprs.extend(inner_exprs)
        return (True, minted_sum)
    except Exception:
        return (False, 0)
