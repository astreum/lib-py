# chain.py
from typing import Callable, Dict, Optional
from .block import Block
from ...storage.models.atom import ZERO32, Atom

class Chain:
    def __init__(self, head_block: Block):
        self.head_block = head_block
        self.validated_upto_block = None
        # Root (genesis) hash for this chain; set by validation setup when known
        self.root: Optional[bytes] = None
        # Fork position: the head hash of the default/current fork for this chain
        self.fork_position: Optional[bytes] = getattr(head_block, "atom_hash", None)
        # Mark the first malicious block encountered during validation; None means not found
        self.malicious_block_hash: Optional[bytes] = None

    def validate(self, storage_get: Callable[[bytes], Atom]) -> Block:
        """Validate the chain from head to genesis and return the root block.

        Incorporates per-block validation (signature on body and timestamp
        monotonicity). Uses a simple cache to avoid duplicate Atom fetches and
        duplicate block decoding during the backward walk.
        """
        # Atom and Block caches for this validation pass
        atom_cache: Dict[bytes, Optional[Atom]] = {}
        block_cache: Dict[bytes, Block] = {}

        def get_cached(k: bytes) -> Optional[Atom]:
            if k in atom_cache:
                return atom_cache[k]
            a = storage_get(k)
            atom_cache[k] = a
            return a

        def load_block(bid: bytes) -> Block:
            if bid in block_cache:
                return block_cache[bid]
            b = Block.from_atom(get_cached, bid)
            block_cache[bid] = b
            return b

        blk = self.head_block
        # Ensure head is in cache if it has a hash
        if getattr(blk, "atom_hash", None):
            block_cache[blk.atom_hash] = blk  # type: ignore[attr-defined]

        # Walk back, validating each block
        while True:
            # Validate current block (signature over body, timestamp rule)
            try:
                blk.validate(get_cached)  # may decode previous but uses cached atoms
            except Exception:
                # record first failure point then propagate
                self.malicious_block_hash = getattr(blk, "atom_hash", None)
                raise

            prev_hash = blk.previous_block_hash if hasattr(blk, "previous_block_hash") else ZERO32
            if prev_hash == ZERO32:
                break
            # Move to previous block using cache-aware loader
            prev_blk = load_block(prev_hash)
            blk.previous_block = prev_blk  # cache the object for any downstream use
            blk = prev_blk

        self.validated_upto_block = blk
        return blk
