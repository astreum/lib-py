from __future__ import annotations

from typing import Optional, Set, Any, Callable, Dict
from .block import Block
from ...storage.models.atom import ZERO32, Atom


class Fork:
    """A branch head within a Chain (same root).

    - head:       current tip block id (bytes)
    - peers:      identifiers (e.g., peer pubkey objects) following this head
    - root:       genesis block id for this chain (optional)
    - validated_upto: earliest verified ancestor (optional)
    - chain_fork_position: the chain's fork anchor relevant to this fork
    """

    def __init__(
        self,
        head: bytes,
    ) -> None:
        self.head: bytes = head
        self.peers: Set[Any] = set()
        self.root: Optional[bytes] = None
        self.validated_upto: Optional[bytes] = None
        self.chain_fork_position: Optional[bytes] = None
        # Mark the first block found malicious during validation; None means not found
        self.malicious_block_hash: Optional[bytes] = None

    def add_peer(self, peer_id: Any) -> None:
        self.peers.add(peer_id)

    def remove_peer(self, peer_id: Any) -> None:
        self.peers.discard(peer_id)

    def validate(
        self,
        storage_get: Callable[[bytes], Optional[object]],
        stop_heads: Optional[Set[bytes]] = None,
    ) -> bool:
        """Validate only up to the chain fork position, not genesis.

        Returns True if self.head descends from self.chain_fork_position (or if
        chain_fork_position is None/equals head), and updates validated_upto to
        that anchor. If stop_heads is provided, returns True early if ancestry
        reaches any of those heads, setting validated_upto to the matched head.
        Returns False if ancestry cannot be confirmed.
        """
        if self.chain_fork_position is None or self.chain_fork_position == self.head:
            self.validated_upto = self.head
            return True
        # Caches to avoid double fetching/decoding
        atom_cache: Dict[bytes, Optional[Atom]] = {}
        block_cache: Dict[bytes, Block] = {}

        def get_cached(k: bytes) -> Optional[Atom]:
            if k in atom_cache:
                return atom_cache[k]
            a = storage_get(k)  # type: ignore[call-arg]
            atom_cache[k] = a  # may be None if missing
            return a

        def load_block(bid: bytes) -> Optional[Block]:
            if bid in block_cache:
                return block_cache[bid]
            try:
                b = Block.from_atom(get_cached, bid)
            except Exception:
                return None
            block_cache[bid] = b
            return b

        blk = load_block(self.head)
        if blk is None:
            # Missing head data: unverifiable, not malicious
            return False
        # Walk up to fork anchor, validating each block signature + timestamp
        while True:
            try:
                blk.validate(get_cached)  # type: ignore[arg-type]
            except Exception:
                # mark the first failure point
                self.malicious_block_hash = blk.atom_hash
                return False

            # Early-exit if we met another known fork head
            if stop_heads and blk.atom_hash in stop_heads:
                self.validated_upto = blk.atom_hash
                return True

            if blk.atom_hash == self.chain_fork_position:
                self.validated_upto = blk.atom_hash
                return True
            
            prev_hash = blk.previous_block_hash if hasattr(blk, "previous_block_hash") else ZERO32
            nxt = load_block(prev_hash)
            if nxt is None:
                return False
            blk.previous_block = nxt  # cache for future use
            blk = nxt
