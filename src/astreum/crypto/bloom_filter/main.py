from __future__ import annotations


class BloomFilter:
    """Incremental tier bloom filter.

    Tiers double in capacity, each element goes into exactly one tier (the
    active one). Test iterates all tiers smallest-to-largest.
    """

    def __init__(self):
        self.count: int = 0
        self.tiers: list[bytearray] = []
        self.start_hash: bytes | None = None
