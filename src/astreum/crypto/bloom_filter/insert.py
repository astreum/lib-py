from __future__ import annotations

from blake3 import blake3

from astreum.crypto.bloom_filter.main import BloomFilter

BITS_PER_ELEMENT = 16  # bits per element in every tier
K = 11  # hash positions per element: round(BITS_PER_ELEMENT * ln 2)
SLICE = 8  # bytes of the digest used for each position


def bloom_digest(element: bytes) -> bytes:
    """Hash an element once for all tiers.

    Args:
        element: Element to hash.

    Returns:
        bytes: ``SLICE * K`` bytes of one blake3 output.
    """
    return blake3(element).digest(length=SLICE * K)


def _bloom_positions(digest: bytes, bits: int) -> list[int]:
    """Compute K bit positions from a digest.

    Each position is one ``SLICE``-byte part of the digest, modulo the tier size.

    Args:
        digest: Output of ``bloom_digest``.
        bits: Number of bits in the tier.

    Returns:
        list[int]: K positions, each below ``bits``.
    """
    return [
        int.from_bytes(digest[SLICE * i : SLICE * (i + 1)], "big") % bits
        for i in range(K)
    ]


def _ensure_tier(bf: BloomFilter) -> int:
    """Ensure enough tiers exist for the next element.

    Args:
        bf: Bloom filter to check.

    Returns:
        int: Description of the return value.
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
        tier_bits = tier_cap * BITS_PER_ELEMENT
        tier_bytes = (tier_bits + 7) // 8
        bf.tiers.append(bytearray(tier_bytes))

    return tier_idx


def bloom_insert(bf: BloomFilter, element: bytes) -> None:
    """Insert an element into the active tier of the bloom filter.

    Args:
        bf: Bloom filter.
        element: Element to insert.

    Returns: None
    """
    tier_idx = _ensure_tier(bf)
    tier = bf.tiers[tier_idx]
    bits = len(tier) * 8
    for pos in _bloom_positions(bloom_digest(element), bits):
        tier[pos // 8] |= (1 << (pos % 8))
    bf.count += 1
