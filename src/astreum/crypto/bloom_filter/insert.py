from __future__ import annotations

from blake3 import blake3

from astreum.crypto.bloom_filter.main import BloomFilter

K = 7  # number of hash functions per element (k = m/n * ln(2) ≈ 6.93, rounds to 7 for <1% FP)


def _bloom_positions(element: bytes, bits: int) -> list[int]:
    """
    Compute K bit positions via double hashing.

    Args:
        element: Element to hash.
        bits: Number of bits in the filter.
    """
    h1 = int.from_bytes(blake3(element + b"\x00").digest()[:4], "big")
    h2 = int.from_bytes(blake3(element + b"\x01").digest()[:4], "big")
    return [(h1 + i * h2) % bits for i in range(K)]


def _ensure_tier(bf: BloomFilter) -> int:
    """
    Ensure enough tiers exist for the next element.

    Args:
        bf: Bloom filter to check.
    """
    # Find which tier this element belongs to
    cum = 0
    tier_idx = 0
    cap = 1
    while cum + cap <= bf.count:
        cum += cap
        tier_idx += 1
        cap = 1 << tier_idx

    # Create tiers as needed
    while len(bf.tiers) <= tier_idx:
        new_idx = len(bf.tiers)
        tier_cap = 1 << new_idx
        tier_bits = tier_cap * 10
        tier_bytes = (tier_bits + 7) // 8
        bf.tiers.append(bytearray(tier_bytes))

    return tier_idx


def bloom_insert(bf: BloomFilter, element: bytes) -> None:
    """
    Insert an element into the active tier of the bloom filter.

    Args:
        bf: Bloom filter.
        element: Element to insert.
    """
    tier_idx = _ensure_tier(bf)
    tier = bf.tiers[tier_idx]
    bits = len(tier) * 8
    for pos in _bloom_positions(element, bits):
        tier[pos // 8] |= (1 << (pos % 8))
    bf.count += 1
