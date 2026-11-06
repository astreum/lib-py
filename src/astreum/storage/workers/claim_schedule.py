from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from fractions import Fraction

from astreum.consensus.block.rate import storage_fee_at_rate
from astreum.consensus.transaction.storage.difficulty import distance_delta, required_bits
from astreum.consensus.validation.validator import SLOT_DURATION_SECONDS

_BASE_TX_FEE = 1  # apply.py:77 — protocol-derived base transaction fee
# Walk size of an 8-field claim receipt (models/receipt.py) with mint=10**12,
# storage_fee=70_000, data_fee=500: 725 bytes, rounded up to the next multiple
# of 10 so the claim cost estimate is not low for real claims.
_RECEIPT_OVERHEAD_BYTES = 730


def pow_budget(hash_rate: float, fair_interval: int | None, held: int) -> int:
    if fair_interval is None:
        return 1
    return max(1, int(hash_rate * fair_interval * SLOT_DURATION_SECONDS / max(1, held)))


def affordable_bits(budget: int) -> int:
    return budget.bit_length() - 1


def claim_cost(rate: Fraction, bytes_per_claim: int, band: int) -> int:
    overhead = _BASE_TX_FEE + storage_fee_at_rate(rate, _RECEIPT_OVERHEAD_BYTES)
    return storage_fee_at_rate(rate, bytes_per_claim) + -(-overhead // band)


def min_elapsed(new_size: int, cost: int, min_profit: int, band: int) -> int:
    return -(-(cost + -(-min_profit // band)) // new_size)


def holder_due(lane_height: int, fair_interval: int | None, inclusion_delay: int) -> int:
    if fair_interval is None:
        return lane_height
    return lane_height + fair_interval - inclusion_delay


def takeover_due(lane_height: int, storage_difficulty: int, delta: int, affordable: int) -> int:
    return lane_height + (1 << max(0, storage_difficulty - delta - affordable))


@dataclass(frozen=True)
class LaneChoice:
    lane: int
    bits: int


def plan_record(
    record,
    me: bytes,
    storage_id: bytes,
    head_height: int,
    storage_difficulty: int,
    fair_interval: int | None,
    inclusion_delay: int,
    affordable: int,
    min_elapsed: int,
) -> tuple[LaneChoice | None, int]:
    for index, lane in enumerate(record.lanes):
        if lane.winner == me:
            due = max(
                holder_due(lane.height, fair_interval, inclusion_delay),
                lane.height + min_elapsed,
            )
            return (LaneChoice(index, 0) if due <= head_height else None), due

    best: tuple[int, int, int] | None = None
    due = None
    for index, lane in enumerate(record.lanes):
        elapsed = head_height + 1 - lane.height
        bits = required_bits(
            sender=me,
            winner=lane.winner,
            storage_id=storage_id,
            elapsed=elapsed,
            storage_difficulty=storage_difficulty,
        )
        if bits <= affordable and head_height - lane.height >= min_elapsed:
            key = (bits, -elapsed, index)
            if best is None or key < best:
                best = key
        delta = distance_delta(sender=me, winner=lane.winner, storage_id=storage_id)
        lane_due = max(
            takeover_due(lane.height, storage_difficulty, delta, affordable),
            lane.height + min_elapsed,
        )
        due = lane_due if due is None else min(due, lane_due)
    choice = None if best is None else LaneChoice(best[2], best[0])
    return choice, due


@dataclass
class ClaimQueue:
    heap: list[tuple[int, bytes]] = field(default_factory=list)
    known: set[bytes] = field(default_factory=set)
    released: set[bytes] = field(default_factory=set)


def queue_add(queue: ClaimQueue, storage_id: bytes, due: int) -> bool:
    if storage_id in queue.known:
        return False
    queue.known.add(storage_id)
    heapq.heappush(queue.heap, (due, storage_id))
    return True


def queue_push(queue: ClaimQueue, storage_id: bytes, due: int) -> None:
    heapq.heappush(queue.heap, (due, storage_id))


def queue_pop_due(queue: ClaimQueue, head_height: int, limit: int) -> list[bytes]:
    ids: list[bytes] = []
    while queue.heap and len(ids) < limit and queue.heap[0][0] <= head_height:
        _, storage_id = heapq.heappop(queue.heap)
        if storage_id not in queue.released:
            ids.append(storage_id)
    return ids
