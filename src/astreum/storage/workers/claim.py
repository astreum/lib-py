from __future__ import annotations

import sys
import time
from fractions import Fraction
from typing import TYPE_CHECKING, Callable

from blake3 import blake3

from astreum.consensus.block.rate import (
    calculate_discount_rate,
    estimate_storage_rate,
    storage_fee_at_rate,
)
from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.consensus.transaction import create_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.storage.model import StorageRecord, storage_record_from_storage
from astreum.consensus.transaction.storage.initial import _single_cell
from astreum.consensus.transaction.storage.difficulty import fair_interval, shared_prefix_bits
from astreum.consensus.transaction.storage.payment import (
    _leading_zero_bits,
    challenge_index as _challenge_index,
)
from astreum.crypto.bloom_search import ERA_SIZE
from astreum.crypto.merkle import create_merkle_tree, generate_merkle_tree_proof
from astreum.expression import Expr, NIL, int_, bytes_, link, ZERO32
from astreum.expression.helpers import bytes_list_to_expr
from astreum.expression.encoding import encode_expr_to_bytes
from astreum.expression.expr import _encode_int
from astreum.storage.cold import get_exprs_from_cold_storage
from astreum.storage.exprs.hot import get_expr_from_hot_storage
from astreum.consensus.validation.validator import SLOT_DURATION_SECONDS
from astreum.storage.records import get_records_from_cold_storage, iter_records_in_cold_storage
from astreum.storage.radix import get_from_radix_tree
from astreum.storage.workers.claim_flow import (
    CapacityLog,
    InFlight,
    band_after_inclusion,
    band_after_loss,
    capacity_min,
    capacity_record,
    in_flight_status,
    lane_value,
    lanes_to_release,
    need,
)
from astreum.storage.workers.claim_schedule import (
    _BASE_TX_FEE,
    _RECEIPT_OVERHEAD_BYTES,
    ClaimQueue,
    LaneChoice,
    affordable_bits,
    claim_cost,
    min_elapsed,
    plan_record,
    pow_budget,
    queue_add,
    queue_pop_due,
    queue_push,
)

if TYPE_CHECKING:
    from astreum.node import Node


# A claim: (storage_id, slot_expr_bytes, proof, nonce, lane).
Claim = tuple[bytes, bytes, list[bytes], int, int]

# A claim with its payout: (storage_id, slot_expr_bytes, proof, nonce, lane, payout).
Entry = tuple[bytes, bytes, list[bytes], int, int, int]

# A claim tx is cut at about this many bytes, well under one STORAGE_FOUND page
# (PAGE_ENTRY_BUDGET), so a peer can fetch it in one response.
MAX_CLAIM_TX_BYTES = 60_000


def measure_hash_rate() -> float:
    """Measure the blake3 hash rate of this machine.

    Hashes ``bytes(160)`` for ``sys.getswitchinterval()`` seconds.

    Returns:
        Hashes per second.
    """
    data = bytes(160)
    interval = sys.getswitchinterval()
    count = 0
    start = time.perf_counter()
    now = start
    while now - start < interval:
        blake3(data).digest()
        count += 1
        now = time.perf_counter()
    return count / max(now - start, 1e-9)


def _find_nonce(
    node: Node,
    prefix: bytes,
    lane: int,
    bits: int,
    max_nonces: int,
    should_stop: Callable[[], bool],
) -> int | None:
    """Search for a PoW nonce in chunks, pausing between them.

    The work hash is ``blake3(prefix + nonce + lane)``. A chunk is
    ``node.storage_claim_hash_rate × sys.getswitchinterval()`` nonces, at least
    one. After each chunk the hash rate is updated from the chunk's speed, the
    stop check runs and the thread yields.

    Args:
        node: A Node instance holding ``storage_claim_hash_rate``.
        prefix: The work-hash input before the nonce.
        lane: The lane number being claimed.
        bits: The leading zero bits the work hash needs.
        max_nonces: The most nonces to try.
        should_stop: Returns True to give up.

    Returns:
        The nonce, or None if the budget ran out or the search was stopped.
    """
    lane_byte = bytes([lane])
    nonce = 0
    while nonce < max_nonces:
        chunk = max(1, int(node.storage_claim_hash_rate * sys.getswitchinterval()))
        end = min(nonce + chunk, max_nonces)
        tried = end - nonce
        start = time.perf_counter()
        while nonce < end:
            work_hash = blake3(prefix + _encode_int(nonce) + lane_byte).digest()
            if _leading_zero_bits(work_hash) >= bits:
                return nonce
            nonce += 1
        node.storage_claim_hash_rate = tried / max(time.perf_counter() - start, 1e-9)
        if should_stop():
            return None
        time.sleep(0)
    return None


def _build_claims(
    node: Node,
    head,
    plans: list[tuple[bytes, StorageRecord, LaneChoice]],
    max_nonces: int,
    should_stop: Callable[[], bool],
) -> list[Entry]:
    """Build the claims for the planned records, reading storage in batches.

    The slot lists come from one records-table read and the missing slots from
    one exprs read. For each record this builds the proof and runs the PoW,
    then pauses. A record that fails a step gives no entry.

    Args:
        node: A Node instance for storage access.
        head: The latest block.
        plans: ``(storage_id, record, choice)`` for each record to claim.
        max_nonces: The most PoW nonces to try for each record.
        should_stop: Returns True to stop the PoW.

    Returns:
        ``(storage_id, slot_bytes, proof, nonce, lane, payout)`` entries.
    """
    blobs = get_records_from_cold_storage(node, [plan[0] for plan in plans]) if plans else {}

    ready = []
    for storage_id, record, choice in plans:
        if not 0 <= choice.lane < len(record.lanes) or record.new_count <= 0:
            continue
        lane_state = record.lanes[choice.lane]
        if len(lane_state.block_hash) != 32:
            continue
        blob = blobs.get(storage_id)
        if blob is None or len(blob) != record.new_count * 32:
            continue
        slot_ids = [blob[i : i + 32] for i in range(0, len(blob), 32)]
        tree = create_merkle_tree(slot_ids)
        if tree.root != record.slots_root:
            continue
        index = _challenge_index(lane_state.block_hash, storage_id, record.new_count, choice.lane)
        ready.append((storage_id, record, choice, tree, index, slot_ids[index]))

    slots: dict[bytes, Expr] = {}
    missing = []
    for item in ready:
        slot_id = item[5]
        expr = get_expr_from_hot_storage(node, slot_id)
        if expr is None:
            missing.append(slot_id)
        else:
            slots[slot_id] = expr
    if missing:
        slots.update(get_exprs_from_cold_storage(node, missing))

    me = node.storage_public_key_bytes
    entries: list[Entry] = []
    for storage_id, record, choice, tree, index, slot_id in ready:
        data_expr = slots.get(slot_id)
        if data_expr is None:
            continue
        slot_bytes = encode_expr_to_bytes(_single_cell(data_expr, node, None))
        proof = generate_merkle_tree_proof(tree, index)
        lane_state = record.lanes[choice.lane]
        prefix = lane_state.block_hash + me + storage_id + slot_id + slot_bytes
        nonce = _find_nonce(node, prefix, choice.lane, choice.bits, max_nonces, should_stop)
        if nonce is not None:
            payout = record.new_size * (head.height - lane_state.height)
            entries.append((storage_id, slot_bytes, proof, nonce, choice.lane, payout))
        time.sleep(0)
    return entries


def _chain_counter(node: Node, block=None) -> int:
    """Read the chain counter of the storage account at a block.

    A missing block, a missing account or a failed read all mean 0.

    Args:
        node: A Node instance with account access.
        block: The block to read. Defaults to ``node.latest_block``.

    Returns:
        The account counter at the block.
    """
    latest_block = block if block is not None else getattr(node, "latest_block", None)
    if latest_block is None:
        return 0
    try:
        account = latest_block.accounts.get_account(
            address=node.storage_public_key_bytes, node=node
        )
        return account.counter if account is not None else 0
    except Exception:
        return 0


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
        chain_id=node.config.chain_id,
        sender=node.storage_public_key_bytes,
        counter=counter,
        recipient=b"\x00" * 32,
        code=TransactionCode.STORAGE_PAYMENT,
        amount=0,
        data=claims_expr,
        secret_key=secret_key,
    )


def _send_batch(
    node: Node,
    head,
    entries: list[Entry],
    counter: int | None,
    rate: Fraction,
) -> tuple[InFlight | None, int]:
    """Build one claim transaction and send it when it pays.

    The batch goes out only when its payouts, less the cost at ``rate``, reach
    ``node.config.storage_claim_min_profit``. The send height, and the counter
    when ``counter`` is None, come from ``node.latest_block`` at the send, not
    from ``head``, because the PoW can take many blocks.

    Args:
        node: A Node instance for sending transactions.
        head: The latest block when the pass began, used for the payout.
        entries: The claim entries with their payouts.
        counter: The transaction counter, or None to read it at the send height.
        rate: The storage rate used for the cost.

    Returns:
        The in-flight record, or None if the batch is not worth sending or the
        send failed, and the size of the built transaction in bytes, or 0 if
        the build failed. The size is given for a dropped batch too, so the
        caller can plan the next batch with the real cost.
    """
    tx_bytes = 0
    try:
        sent = getattr(node, "latest_block", None) or head
        if counter is None:
            counter = _chain_counter(node, sent)
        tx = _build_multi_claim_tx(node, [entry[:5] for entry in entries], node.storage_secret_key, counter)
        tx_bytes = _tree_bytes(tx.expr())
        cost = (
            _BASE_TX_FEE
            + storage_fee_at_rate(rate, tx_bytes)
            + storage_fee_at_rate(rate, _RECEIPT_OVERHEAD_BYTES)
        )
        total_payout = sum(entry[5] for entry in entries)
        if total_payout - cost < node.config.storage_claim_min_profit:
            node.logger.info(
                "Claim worker: batch unprofitable (%d payouts, %d cost); dropped",
                total_payout,
                cost,
            )
            return None, tx_bytes
        from astreum.consensus.transaction.send import send_transaction
        send_transaction(node, tx)
        node.logger.info(
            "Claim worker: submitted %d claim(s) in tx %s (payout %d, cost %d)",
            len(entries),
            tx.hash.hex() if tx.hash else "unknown",
            total_payout,
            cost,
        )
        return InFlight(counter, sent.height, entries, tx_bytes=tx_bytes), tx_bytes
    except Exception as exc:
        node.logger.exception("Claim worker: failed to submit claims: %s", exc)
        return None, tx_bytes


def _claim_step(
    node: Node,
    head,
    storage_tree,
    ids: list[bytes],
    queue: ClaimQueue,
    held: dict[bytes, tuple[int, int]],
    rate: Fraction,
    bytes_per_claim: int,
    should_stop: Callable[[], bool],
) -> list[Entry]:
    """Plan and build the claims for the given records.

    Each record is read from the storage tree and planned. A record with no
    claim now goes back on the queue at its due height. The records with a
    lane choice are built in one batch. A record that gave an entry is not
    queued, because the loop queues it when its transaction is resolved.

    Args:
        node: A Node instance for storage access.
        head: The latest block.
        storage_tree: The radix tree of storage records.
        ids: The storage ids to look at.
        queue: The claim queue.
        held: ``storage_id -> (new_size, shared prefix bits)`` for each held lane.
        rate: The storage rate used for the cost.
        bytes_per_claim: The bytes one claim adds to a transaction.
        should_stop: Returns True to stop the PoW.

    Returns:
        The claim entries built.
    """
    me = node.storage_public_key_bytes
    band = node.storage_claim_band
    interval = fair_interval(head)
    retry = head.height + (interval or 1)
    cost = claim_cost(rate, bytes_per_claim, band)
    records: list[tuple[bytes, StorageRecord]] = []
    for storage_id in ids:
        record = None
        try:
            record_head = get_from_radix_tree(storage_tree, node, storage_id)
            if record_head is not None:
                record = storage_record_from_storage(node, record_head.hash())
        except Exception as exc:
            node.logger.exception("Claim worker: failed to read record %s: %s", storage_id.hex(), exc)
        if record is None:
            held.pop(storage_id, None)
            queue_push(queue, storage_id, retry)
            continue
        if record.new_size <= 0:
            queue_push(queue, storage_id, retry)
            continue
        if any(lane.winner == me for lane in record.lanes):
            held[storage_id] = (record.new_size, shared_prefix_bits(me, storage_id))
        else:
            held.pop(storage_id, None)
        records.append((storage_id, record))

    budget = pow_budget(node.storage_claim_hash_rate, interval, len(held))
    affordable = affordable_bits(budget)
    plans: list[tuple[bytes, StorageRecord, LaneChoice]] = []
    for storage_id, record in records:
        choice, due = plan_record(
            record,
            me,
            storage_id,
            head.height,
            head.storage_difficulty,
            interval,
            node.storage_claim_inclusion_delay,
            affordable,
            min_elapsed(record.new_size, cost, node.config.storage_claim_min_profit, band),
        )
        if choice is None:
            queue_push(queue, storage_id, due)
        else:
            plans.append((storage_id, record, choice))

    if not plans:
        return []
    entries = _build_claims(node, head, plans, budget, should_stop)
    built = {entry[0] for entry in entries}
    for storage_id, _, _ in plans:
        if storage_id not in built:
            queue_push(queue, storage_id, retry)
    return entries


def claim_storage(node: Node) -> None:
    """Run the claim worker as a stream of batched claim transactions.

    Returns at once when ``node.is_storage_provider`` is false. Otherwise it
    loops until ``communication_stop_event`` is set. Each pass reads the head,
    queues new and stored records, resolves the transaction in flight, releases
    lanes the node cannot serve, then builds and sends the next batch. One
    transaction is in flight at a time.

    Args:
        node: A Node instance with ``communication_stop_event``.

    Returns:
        None.
    """
    if not getattr(node, "is_storage_provider", False):
        return

    stop = node.communication_stop_event
    if node.storage_claim_hash_rate is None:
        node.storage_claim_hash_rate = measure_hash_rate()

    queue = ClaimQueue()
    held: dict[bytes, tuple[int, int]] = {}
    in_flight: InFlight | None = None
    pending: list[Entry] = []
    capacity = CapacityLog()
    scan = iter_records_in_cold_storage(node, batch=node.storage_claim_band)
    bytes_per_claim = 0
    tree_head = None
    storage_tree = None
    storage_rate: Fraction | None = None

    while not stop.is_set():
        head = node.latest_block
        popped: list[bytes] = []
        fair = None
        try:
            if head is None:
                stop.wait(SLOT_DURATION_SECONDS)
                continue
            if head is not tree_head:
                account = head.accounts.get_account(STORAGE_ADDRESS, node)
                try:
                    head_rate = estimate_storage_rate(head, node.config.storage_claim_fee_window)
                except ValueError:
                    head_rate = None
                storage_tree = account.data if account is not None else None
                storage_rate = head_rate
                tree_head = head
            if storage_rate is None:
                stop.wait(SLOT_DURATION_SECONDS)
                continue

            band = node.storage_claim_band
            while node.new_record_ids:
                queue_add(queue, node.new_record_ids.popleft(), head.height)
            if scan is not None:
                try:
                    for storage_id in scan:
                        queue_add(queue, storage_id, head.height)
                    scan = None
                except Exception as exc:
                    node.logger.exception("Claim worker: record scan failed: %s", exc)
                    scan = iter_records_in_cold_storage(node, batch=band)

            fair = fair_interval(head)
            if fair is not None:
                fair = max(1, fair)
            if in_flight is not None:
                status = in_flight_status(in_flight, _chain_counter(node), head.height)
                if status == "pending":
                    stop.wait(SLOT_DURATION_SECONDS)
                    continue
                if status == "included":
                    delay = max(1, head.height - in_flight.sent_height)
                    node.storage_claim_inclusion_delay = delay
                    capacity_record(capacity, head.height, band / delay)
                    band = band_after_inclusion(band, delay)
                    node.storage_claim_band = band
                    for entry in in_flight.entries:
                        queue_push(queue, entry[0], head.height)
                    in_flight = None
                else:
                    band = band_after_loss(band)
                    node.storage_claim_band = band
                    lost = in_flight
                    in_flight, tx_bytes = _send_batch(
                        node, head, lost.entries, lost.counter, storage_rate
                    )
                    if tx_bytes:
                        bytes_per_claim = tx_bytes // len(lost.entries)
                    if in_flight is None:
                        for entry in lost.entries:
                            queue_push(queue, entry[0], head.height + (fair or 1))
                    continue

            if fair is not None and need(len(held), fair) > capacity_min(capacity, head.height, fair):
                try:
                    discount_rate = calculate_discount_rate(head, node, range_blocks=ERA_SIZE)
                except ValueError:
                    discount_rate = 0.0
                cost = claim_cost(storage_rate, bytes_per_claim, band)
                items = [
                    (storage_id, lane_value(new_size, fair, discount_rate, cost), prefix_bits)
                    for storage_id, (new_size, prefix_bits) in held.items()
                ]
                for storage_id in lanes_to_release(items, fair, capacity_min(capacity, head.height, fair)):
                    queue.released.add(storage_id)
                    held.pop(storage_id)

            pending = [entry for entry in pending if entry[0] not in queue.released]
            if not pending:
                popped = queue_pop_due(queue, head.height, band)
            entries = pending or _claim_step(
                node,
                head,
                storage_tree,
                popped,
                queue,
                held,
                storage_rate,
                bytes_per_claim,
                stop.is_set,
            )
            if not entries:
                stop.wait(SLOT_DURATION_SECONDS)
                continue
            groups = _split_claims(entries)
            pending = [entry for group in groups[1:] for entry in group]
            in_flight, tx_bytes = _send_batch(node, head, groups[0], None, storage_rate)
            if tx_bytes:
                bytes_per_claim = tx_bytes // len(groups[0])
            if in_flight is None:
                for entry in groups[0]:
                    queue_push(queue, entry[0], head.height + (fair or 1))
            time.sleep(0)
        except Exception as exc:
            node.logger.exception("Claim worker: pass failed: %s", exc)
            if popped:
                kept = {storage_id for _, storage_id in queue.heap}
                if in_flight is not None:
                    kept.update(entry[0] for entry in in_flight.entries)
                kept.update(entry[0] for entry in pending)
                for storage_id in popped:
                    if storage_id not in kept:
                        queue_push(queue, storage_id, head.height + (fair or 1))
            stop.wait(SLOT_DURATION_SECONDS)
