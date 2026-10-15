from __future__ import annotations

import time
from typing import TYPE_CHECKING

from blake3 import blake3

from astreum.consensus.block.rate import calculate_storage_fee
from astreum.consensus.transaction import create_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.storage.model import StorageRecord, storage_record_from_storage
from astreum.consensus.transaction.storage.initial import _single_cell
from astreum.consensus.transaction.storage.payment import (
    _leading_zero_bits,
    _required_bits,
    challenge_index as _challenge_index,
)
from astreum.crypto.merkle import create_merkle_tree, generate_merkle_tree_proof
from astreum.crypto.bloom_search import ERA_SIZE
from astreum.expression import Expr, NIL, int_, bytes_, link, ZERO32
from astreum.expression.helpers import bytes_list_to_expr
from astreum.expression.encoding import encode_expr_to_bytes
from astreum.expression.expr import _encode_int
from astreum.storage.exprs import get_expr_from_local_storage
from astreum.storage.records import iter_records_in_cold_storage, get_record_from_cold_storage
from astreum.storage.radix import get_from_radix_tree

if TYPE_CHECKING:
    from astreum.node import Node


# A claim: (storage_id, slot_expr_bytes, proof, nonce, lane).
Claim = tuple[bytes, bytes, list[bytes], int, int]

# A claim tx is cut at about this many bytes, well under one STORAGE_FOUND page
# (PAGE_ENTRY_BUDGET), so a peer can fetch it in one response.
MAX_CLAIM_TX_BYTES = 60_000


def _compute_pow_and_challenge(
    node: Node,
    storage_id: bytes,
    record: StorageRecord,
    lane: int,
) -> Claim | None:
    """Compute a PoW claim for one lane of the given record.

    The challenged slot's id is read from the record's slot list in the records
    table (byte offset = index * 32). The tree over that list is built in
    memory and gives the slot's proof against ``record.slots_root``. Returns
    ``(storage_id, slot_expr_bytes, proof, nonce, lane)`` or None if unavailable.

    Args:
        node: A Node instance for storage and network access.
        storage_id: The 32-byte storage ID (block hash).
        record: The StorageRecord to claim for.
        lane: The lane number to claim.

    Returns:
        A Claim tuple, or None if the record cannot be claimed.
    """
    if not 0 <= lane < len(record.lanes):
        return None
    lane_state = record.lanes[lane]
    last_payment_block_hash = lane_state.block_hash
    if len(last_payment_block_hash) != 32:
        return None
    if record.new_count <= 0:
        return None

    challenge_index = _challenge_index(last_payment_block_hash, storage_id, record.new_count, lane)

    # The slot list from the records table
    value = get_record_from_cold_storage(node, storage_id)
    if value is None or len(value) != record.new_count * 32:
        return None
    slot_ids = [value[i : i + 32] for i in range(0, len(value), 32)]
    tree = create_merkle_tree(slot_ids)
    if tree.root != record.slots_root:
        return None
    slot_id = slot_ids[challenge_index]
    proof = generate_merkle_tree_proof(tree, challenge_index)

    # The slot's data, as the single cell the record walk slotted
    data_expr = get_expr_from_local_storage(node, slot_id)
    if data_expr is None:
        return None
    fetched_data_bytes = encode_expr_to_bytes(_single_cell(data_expr, node, None))
    sender_bytes = node.storage_public_key_bytes

    # Compute required bits
    required_bits = _required_bits(
        sender=sender_bytes,
        last_payment_winner=lane_state.winner,
        block_height=node.latest_block.height,
        last_payment_height=lane_state.height,
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
            + bytes([lane])
        ).digest()
        if _leading_zero_bits(work_hash) >= required_bits:
            return storage_id, fetched_data_bytes, proof, nonce, lane
        nonce += 1

    return None


def _next_claim_counter(node: Node) -> int:
    """Resolve the counter for the next claim tx.

    Uses the greater of the on-chain sender-account counter and the
    node-local ``next_claim_counter`` (bumped after each successful send so
    consecutive eras don't wait for on-chain confirmation). A missing
    account, no latest block, or a fetch failure all mean 0.

    Args:
        node: A Node instance with account access.

    Returns:
        The next transaction counter.
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


def _claim_expr(claim: Claim) -> Expr:
    """Convert a Claim tuple to an S-expression.

    Args:
        claim: A (storage_id, slot_bytes, proof, nonce, lane) tuple.

    Returns:
        An Expr representing the claim.
    """
    storage_id, slot_bytes, proof, nonce, lane = claim
    return link(
        bytes_(storage_id),
        link(
            bytes_(slot_bytes),
            link(
                bytes_list_to_expr(proof),
                link(int_(nonce), link(int_(lane), NIL)),
            ),
        ),
    )


def _tree_bytes(expr: Expr) -> int:
    """Return the encoded size of every distinct cell of a loaded expr.

    This is what a peer fetching the whole tree receives.

    Args:
        expr: The expression to measure.

    Returns:
        Total encoded size in bytes.
    """
    seen: set[bytes] = set()
    total = 0
    stack = [expr]
    while stack:
        e = stack.pop()
        if e is None:
            continue
        h = e.hash()
        if h in seen:
            continue
        seen.add(h)
        total += len(encode_expr_to_bytes(e))
        if e.base == "link":
            stack.append(e.tail)
            stack.append(e.head)
    return total


def _split_claims(entries: list[tuple]) -> list[list[tuple]]:
    """Group claim entries greedily into transactions of about ``MAX_CLAIM_TX_BYTES``.

    Each entry is ``(*claim, payout)``, six items. A claim bigger than the limit on its
    own gets a transaction of its own.

    Args:
        entries: List of claim tuples.

    Returns:
        List of claim groups.
    """
    groups: list[list[tuple]] = []
    current: list[tuple] = []
    size = 0
    for entry in entries:
        entry_size = _tree_bytes(_claim_expr(entry[:5]))
        if current and size + entry_size > MAX_CLAIM_TX_BYTES:
            groups.append(current)
            current, size = [], 0
        current.append(entry)
        size += entry_size
    if current:
        groups.append(current)
    return groups


def _build_multi_claim_tx(
    node: Node,
    claims: list[Claim],
    secret_key,
    counter: int,
) -> object:
    """Build a signed STORAGE_PAYMENT transaction with the given claims.

    Args:
        node: A Node instance with chain ID.
        claims: List of Claim tuples.
        secret_key: The signing key.
        counter: The transaction counter.

    Returns:
        The signed transaction object.
    """
    claims_expr = NIL
    for claim in reversed(claims):
        claims_expr = link(_claim_expr(claim), claims_expr)

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
) -> list[tuple[bytes, bytes, list[bytes], int, int, int]]:
    """Evaluate every record in the records table and build eligible claims.

    Returns ``(storage_id, slot_expr_bytes, proof, nonce, lane, payout)`` tuples,
    where ``payout = new_size × (block_height − lane height)``
    mirrors the reward calculation in ``storage/payment.py`` so the submit
    gate can check the bundle's aggregate economics.

    Args:
        node: A Node instance for storage access.
        latest_block: The latest block.
        spacing_eras: Dict tracking spacing backoff per storage ID.

    Returns:
        List of claim tuples with payouts.
    """
    from astreum.consensus.constants import STORAGE_ADDRESS

    claims_to_make: list[tuple[bytes, bytes, list[bytes], int, int, int]] = []
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

        record = storage_record_from_storage(node, contract_head.hash())
        if record is None:
            continue

        current_spacing = spacing_eras.get(storage_id, 1)

        def eras_elapsed(lane: int) -> int:
            return (latest_block.height - record.lanes[lane].height) // ERA_SIZE

        mine = next(
            (i for i, lane in enumerate(record.lanes) if lane.winner == node.storage_public_key_bytes),
            None,
        )
        if mine is not None:
            # We hold a lane: claim it with the same spacing back-off as before.
            # A holder never takes the other lane.
            if eras_elapsed(mine) >= current_spacing:
                claim = _compute_pow_and_challenge(node, storage_id, record, mine)
                if claim is not None:
                    payout = record.new_size * (latest_block.height - record.lanes[mine].height)
                    claims_to_make.append((*claim, payout))
                    spacing_eras[storage_id] = min(current_spacing + 1, 4)
        else:
            # Takeover: only after 5 eras (fib(8)=21, ~2Mx harder), on the lane
            # that waited longest. Lane 0 wins a tie.
            spacing_eras[storage_id] = 1  # reset
            open_lanes = [i for i in range(len(record.lanes)) if eras_elapsed(i) >= 5]
            if open_lanes:
                lane = max(open_lanes, key=lambda i: (eras_elapsed(i), -i))
                claim = _compute_pow_and_challenge(node, storage_id, record, lane)
                if claim is not None:
                    payout = record.new_size * (latest_block.height - record.lanes[lane].height)
                    claims_to_make.append((*claim, payout))
    return claims_to_make


_BASE_TX_FEE = 1  # apply.py:77 — protocol-derived base transaction fee
# Walk size of a 7-field receipt (models/receipt.py): 17 cells, 717 bytes with
# one-byte ints, the size the block record counts for it.
_RECEIPT_OVERHEAD_BYTES = 720


def _submit_claims(node: Node, entries: list[tuple]) -> None:
    """Send the given claims as STORAGE_PAYMENT transactions.

    Each claim is a ``(*claim, payout)`` entry. Transactions are about
    ``MAX_CLAIM_TX_BYTES`` each, sent only if the payout covers protocol cost.

    Args:
        node: A Node instance for sending transactions.
        entries: List of claim tuples with payouts.

    Returns: None
    """
    if not entries:
        return
    latest_block = getattr(node, "latest_block", None)
    if latest_block is None:
        return
    for group in _split_claims(entries):
        if not _submit_claim_group(node, latest_block, group):
            return


def _submit_claim_group(node: Node, latest_block, entries: list[tuple]) -> bool:
    """Build and send one claim transaction.

    Returns False to stop submitting (the cost cannot be computed yet).
    An unprofitable or failed group is dropped and the next one still goes.

    Args:
        node: A Node instance for sending transactions.
        latest_block: The latest block for fee calculation.
        entries: List of claim tuples with payouts.

    Returns:
        True to continue, False to stop submitting.
    """
    try:
        claims = [entry[:5] for entry in entries]
        counter = _next_claim_counter(node)
        tx = _build_multi_claim_tx(node, claims, node.storage_secret_key, counter)
        try:
            cost = (
                _BASE_TX_FEE
                + calculate_storage_fee(latest_block, _tree_bytes(tx.expr()))
                + calculate_storage_fee(latest_block, _RECEIPT_OVERHEAD_BYTES)
            )
        except ValueError:
            # cumulative_total_fee <= 0 (young/odd block, rate.py:36-37)
            return False  # pass skipped; retried next era
        total_payout = sum(entry[5] for entry in entries)
        if total_payout < cost:
            node.logger.info(
                "Claim worker: bundle unprofitable (%d payouts < %d cost); dropped",
                total_payout,
                cost,
            )
            return True  # drop, retry next era
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
    return True


def claim_storage(node: Node) -> None:
    """Submit storage reward claims on era boundaries.

    Runs as a daemon thread.  Returns at once when ``node.is_storage_provider``
    is false.  On each era boundary (every ``ERA_SIZE``
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
        None.  Runs indefinitely until ``communication_stop_event`` fires.
    """
    if not getattr(node, "is_storage_provider", False):
        return

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
