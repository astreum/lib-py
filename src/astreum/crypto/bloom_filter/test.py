from __future__ import annotations

from astreum.crypto.bloom_filter.insert import _bloom_positions, bloom_digest
from astreum.crypto.bloom_filter.main import BloomFilter


def bloom_test(bf: BloomFilter, element: bytes) -> bool:
    """Test if an element may be present.

    Args:
        bf: Bloom filter.
        element: Element to test.

    Returns:
        bool: Description of the return value.
    """
    digest = bloom_digest(element)
    for tier in bf.tiers:
        bits = len(tier) * 8
        positions = _bloom_positions(digest, bits)
        if all((tier[pos // 8] >> (pos % 8)) & 1 for pos in positions):
            return True
    return False
