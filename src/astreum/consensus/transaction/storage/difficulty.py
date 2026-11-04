from __future__ import annotations

from astreum.expression import ZERO32
from astreum.consensus.constants import PROVIDERS_PER_RECORD
from astreum.consensus.block.rate_window import windowed_claims


def shared_prefix_bits(a: bytes, b: bytes) -> int:
    width = len(a) * 8
    return width - int.from_bytes(bytes(x ^ y for x, y in zip(a, b)), "big").bit_length()


def distance_delta(*, sender: bytes, winner: bytes, storage_id: bytes) -> int:
    if winner == ZERO32:
        return 0
    return shared_prefix_bits(sender, storage_id) - shared_prefix_bits(winner, storage_id)


def required_bits(
    *,
    sender: bytes,
    winner: bytes,
    storage_id: bytes,
    elapsed: int,
    storage_difficulty: int,
) -> int:
    if sender == winner:
        return 0
    delta = distance_delta(sender=sender, winner=winner, storage_id=storage_id)
    return max(0, storage_difficulty - (max(1, elapsed).bit_length() - 1) - delta)


def takeover_position(elapsed: int, delta: int) -> int:
    if delta < 0:
        return elapsed >> -delta
    return elapsed << delta


def takeover_vote(position: int, fair_interval: int | None) -> int:
    if fair_interval is None or position == fair_interval:
        return 0
    return 1 if position < fair_interval else -1


def next_storage_difficulty(parent: int, votes: int) -> int:
    return max(0, parent + (votes > 0) - (votes < 0))


def parent_storage_difficulty(block) -> int:
    previous_block = getattr(block, "previous_block", None)
    if previous_block is None:
        return 0
    return previous_block.storage_difficulty


def fair_interval(parent) -> int | None:
    if parent is None:
        return None
    stats = getattr(parent, "statistics", None)
    if not stats:
        return None

    lanes = PROVIDERS_PER_RECORD * (getattr(parent, "height", 0) or 0)
    window = 0
    claims = 0
    for i in range(len(stats) - 1):
        window = 1 << i
        claims = windowed_claims(parent, window) or 0
        if claims >= lanes:
            break

    if claims <= 0:
        return None
    return lanes * window // claims
