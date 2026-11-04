from __future__ import annotations


def update_statistics(
    height: int,
    prev_statistics: list | None,
    delta_fee: int,
    delta_stake: int,
    delta_claims: int,
) -> list[tuple[int, int, int, int, int, int]]:
    stats: list[tuple[int, int, int, int, int, int]] = []

    # Range 0 — all-time cumulative, never rotates
    if prev_statistics:
        fee, stake, claims = prev_statistics[0][:3]
    else:
        fee, stake, claims = 0, 0, 0
    stats.append((fee + delta_fee, stake + delta_stake, claims + delta_claims, 0, 0, 0))

    # Ranges 1, 2, 4, … — pow2 windows
    max_entries = height.bit_length() if height > 0 else 0
    for i in range(max_entries):
        R = 1 << i
        pi = i + 1  # position in stats (0 is range 0)
        new_entry = not (prev_statistics and pi < len(prev_statistics))
        if new_entry:
            # Window born at height == R: history 0..R-1 is exactly one
            # completed window of size R — seed prev from all-time totals.
            if prev_statistics and height == R:
                prev_fee, prev_stake, prev_claims = prev_statistics[0][:3]
            else:
                prev_fee, prev_stake, prev_claims = 0, 0, 0
            curr_fee, curr_stake, curr_claims = 0, 0, 0
        else:
            prev_fee, prev_stake, curr_fee, curr_stake, prev_claims, curr_claims = prev_statistics[pi]

        if height % R == 0 and height > 0:
            if not new_entry:
                prev_fee, prev_stake, prev_claims = curr_fee, curr_stake, curr_claims
            curr_fee, curr_stake, curr_claims = delta_fee, delta_stake, delta_claims
        else:
            curr_fee += delta_fee
            curr_stake += delta_stake
            curr_claims += delta_claims

        stats.append((prev_fee, prev_stake, curr_fee, curr_stake, prev_claims, curr_claims))

    return stats


def windowed_rate_fraction(
    block: object,
    duration: int,
) -> tuple[int, int] | None:
    previous_block = getattr(block, "previous_block", None)
    if previous_block is None:
        return None

    stats = getattr(previous_block, "statistics", None)
    if not stats:
        return None

    if duration == 0:
        fee, stake = stats[0][:2]
        if stake <= 0:
            return None
        return fee, stake

    if duration <= 0 or (duration & (duration - 1)) != 0:
        return None

    idx = duration.bit_length()
    if idx >= len(stats):
        return None

    prev_fee, prev_stake, curr_fee, curr_stake = stats[idx][:4]
    if prev_stake <= 0:
        return None

    previous_height = getattr(previous_block, "height", 0) or 0
    R = duration
    progress = previous_height % R
    alpha = progress / R

    blended_fee = int(prev_fee * (1 - alpha) + curr_fee * alpha)
    blended_stake = int(prev_stake * (1 - alpha) + curr_stake * alpha)
    if blended_stake <= 0:
        return None

    return blended_fee, blended_stake


def windowed_claims(
    parent: object,
    duration: int,
) -> int | None:
    if parent is None:
        return None

    stats = getattr(parent, "statistics", None)
    if not stats:
        return None

    if duration <= 0 or (duration & (duration - 1)) != 0:
        return None

    idx = duration.bit_length()
    if idx >= len(stats):
        return None

    prev_claims, curr_claims = stats[idx][4:6]

    parent_height = getattr(parent, "height", 0) or 0
    alpha = (parent_height % duration) / duration

    return int(prev_claims * (1 - alpha) + curr_claims * alpha)
