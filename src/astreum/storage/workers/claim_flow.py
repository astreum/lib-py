from __future__ import annotations

from dataclasses import dataclass, field

from astreum.crypto.bloom_search import ERA_SIZE


@dataclass
class InFlight:
    counter: int
    sent_height: int
    entries: list[tuple]
    tx_bytes: int = 0


def in_flight_status(in_flight: InFlight, chain_counter: int, head_height: int) -> str:
    if chain_counter > in_flight.counter:
        return "included"
    if head_height >= in_flight.sent_height + 2:
        return "lost"
    return "pending"


def band_after_inclusion(band: int, delay: int) -> int:
    return band + 1 if delay == 1 else band


def band_after_loss(band: int) -> int:
    return max(1, band // 2)


@dataclass
class CapacityLog:
    samples: list[tuple[int, float]] = field(default_factory=list)


def capacity_record(log: CapacityLog, height: int, capacity: float) -> None:
    log.samples.append((height, capacity))


def capacity_min(log: CapacityLog, head_height: int, fair_interval: int) -> float:
    log.samples[:] = [s for s in log.samples if s[0] >= head_height - fair_interval]
    if not log.samples:
        return float("inf")
    return min(capacity for _, capacity in log.samples)


def need(held: int, fair_interval: int) -> float:
    return held / fair_interval


def lane_value(new_size: int, fair_interval: int, rate: float, claim_cost: float) -> float:
    return new_size * fair_interval * (1 - rate) ** (fair_interval / ERA_SIZE) - claim_cost


def lanes_to_release(
    items: list[tuple[bytes, float, int]], fair_interval: int, capacity: float
) -> list[bytes]:
    released: list[bytes] = []
    for storage_id, _, _ in sorted(items, key=lambda item: (item[1], item[2])):
        if need(len(items) - len(released), fair_interval) <= capacity:
            break
        released.append(storage_id)
    return released
