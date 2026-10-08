from __future__ import annotations

from typing import Optional, Set, Any
from astreum.consensus.models.block import Block
from astreum.consensus.block.encoding.decode import get_block_from_storage
from astreum.expression import ZERO32
from astreum.consensus.block.verify import verify_block_head, verify_block_transactions


class Fork:
    """A branch head within a Chain (same root).

    - head:       current tip block id (bytes)
    - peers:      identifiers (e.g., peer pubkey objects) following this head
    - root:       genesis block id for this chain (optional)
    - verified_up_to: earliest verified ancestor (optional)
    - chain_fork_position: the chain's fork anchor relevant to this fork
    """

    def __init__(
        self,
        *,
        head: bytes,
        root: bytes = ZERO32,
        verified_up_to: Optional[bytes] = None,
        chain_fork_position: Optional[bytes] = None,
        reached_genesis: bool = False,
        malicious_block_hash: Optional[bytes] = None,
    ) -> None:
        self.head: bytes = head
        self.peers: Set[Any] = set()
        self.root: bytes = root
        self.verified_up_to: Optional[bytes] = verified_up_to
        self.chain_fork_position: Optional[bytes] = chain_fork_position
        self.reached_genesis: bool = bool(reached_genesis)
        self.malicious_block_hash: Optional[bytes] = malicious_block_hash

    def to_dict(self) -> dict[str, Any]:
        """Convert fork to dictionary.

        Args:
            None.

        Returns:
            Dictionary representation of the fork.
        """
        return {
            "head": self.head,
            "verified_up_to": self.verified_up_to,
            "chain_fork_position": self.chain_fork_position,
            "reached_genesis": self.reached_genesis,
            "malicious_block_hash": self.malicious_block_hash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Fork:
        """Create a Fork from a dictionary.

        Args:
            payload: Dictionary with fork data.

        Returns:
            The Fork instance.

        Raises:
            TypeError: If fields have wrong types.
        """
        head = payload.get("head")
        if not isinstance(head, (bytes, bytearray)):
            raise TypeError("fork head must be bytes")

        verified_up_to = payload.get("verified_up_to")
        if verified_up_to is not None and not isinstance(
            verified_up_to, (bytes, bytearray)
        ):
            raise TypeError("fork verified_up_to must be bytes or None")

        chain_fork_position = payload.get("chain_fork_position")
        if chain_fork_position is not None and not isinstance(
            chain_fork_position, (bytes, bytearray)
        ):
            raise TypeError("fork chain_fork_position must be bytes or None")

        reached_genesis = payload.get("reached_genesis")
        if not isinstance(reached_genesis, bool):
            raise TypeError("fork reached_genesis must be bool")

        malicious_block_hash = payload.get("malicious_block_hash")
        if malicious_block_hash is not None and not isinstance(
            malicious_block_hash, (bytes, bytearray)
        ):
            raise TypeError("fork malicious_block_hash must be bytes or None")

        return cls(
            head=head,
            verified_up_to=verified_up_to
            if isinstance(verified_up_to, (bytes, bytearray))
            else None,
            chain_fork_position=chain_fork_position
            if isinstance(chain_fork_position, (bytes, bytearray))
            else None,
            reached_genesis=reached_genesis,
            malicious_block_hash=malicious_block_hash
            if isinstance(malicious_block_hash, (bytes, bytearray))
            else None,
        )

    def add_peer(self, peer_id: Any) -> None:
        """Add a peer to the fork.

        Args:
            peer_id: The peer identifier.
        """
        self.peers.add(peer_id)

    def remove_peer(self, peer_id: Any) -> None:
        """Remove a peer from the fork.

        Args:
            peer_id: The peer identifier.
        """
        self.peers.discard(peer_id)

    def verify(self, node: Any) -> bool:
        """Verify the fork.

        Args:
            node: The node to verify on.

        Returns:
            True if verification succeeds, False otherwise.
        """
        def _hex(value: Optional[bytes]) -> str:
            """Convert bytes to hex string.

            Args:
                value: The bytes value.

            Returns:
                Hex string or str(value).
            """
            if isinstance(value, (bytes, bytearray)):
                return value.hex()
            return str(value)

        node.logger.debug("Fork verify start head=%s", _hex(self.head))

        anchor_hash: Optional[bytes] = None
        anchor_kind: Optional[str] = None
        intersection_fork_head: Optional[bytes] = None
        anchor_validated = False

        def is_on_other_fork_path(target_hash: bytes) -> Optional[bytes]:
            """Check if target is on another fork path.

            Args:
                target_hash: The hash to check.

            Returns:
                Other fork head if found, else None.
            """
            for other_head in node.forks:
                if other_head == self.head:
                    continue
                blk_hash = other_head
                seen: Set[bytes] = set()
                while blk_hash and blk_hash not in seen:
                    seen.add(blk_hash)
                    if blk_hash == target_hash:
                        return other_head
                    try:
                        blk = get_block_from_storage(astreum_node=node, block_hash=blk_hash)
                    except Exception:
                        node.logger.debug("Fork path lookup failed loading block=%s", _hex(blk_hash))
                        blk = None
                    if blk is None:
                        break
                    prev = getattr(blk, "previous_block_hash", ZERO32) or ZERO32
                    if prev == ZERO32:
                        break
                    blk_hash = prev
            return None

        current_block_hash = self.head
        current_block: Optional[Block] = None
        while current_block_hash:
            try:
                previous_block = get_block_from_storage(astreum_node=node, block_hash=current_block_hash)
            except Exception:
                node.logger.debug("Fork verify failed loading block=%s", _hex(current_block_hash))
                previous_block = None
            if previous_block is None:
                if current_block_hash == self.head:
                    return False
                if current_block and current_block.expr_id:
                    self.verified_up_to = current_block.expr_id
                self.malicious_block_hash = (
                    current_block.expr_id if current_block else current_block_hash
                )
                node.logger.warning("Fork verify failed missing block=%s pending=%s", _hex(current_block_hash), _hex(current_block.expr_id) if current_block else None)
                return False

            if current_block is not None:
                current_block.previous_block = previous_block
                if not verify_block_head(node, current_block):
                    self.malicious_block_hash = current_block_hash
                    node.logger.warning("Fork verify failed header block=%s previous_block=%s", _hex(current_block.expr_id), _hex(previous_block.expr_id))
                    return False
                
                if anchor_hash is not None and current_block.expr_id == anchor_hash:
                    anchor_validated = True
                    node.logger.debug("Fork verify reached anchor=%s kind=%s", _hex(anchor_hash), anchor_kind)
                    break

            if anchor_hash is None:
                if current_block_hash == self.root:
                    anchor_hash = current_block_hash
                    anchor_kind = "root"
                    node.logger.debug("Fork verify anchor root=%s", _hex(anchor_hash))
                if current_block_hash in node.forks and current_block_hash != self.head:
                    anchor_hash = current_block_hash
                    anchor_kind = "fork_head"
                    node.logger.debug("Fork verify anchor fork_head=%s", _hex(anchor_hash))
                else:
                    other_head = is_on_other_fork_path(current_block_hash)
                    if other_head:
                        anchor_hash = current_block_hash
                        anchor_kind = "intersection"
                        intersection_fork_head = other_head
                        node.logger.debug("Fork verify anchor intersection=%s other_head=%s", _hex(anchor_hash), _hex(other_head))
                    else:
                        prev_hash = getattr(previous_block, "previous_block_hash", ZERO32) or ZERO32
                        if prev_hash == ZERO32:
                            self.reached_genesis = True
                            anchor_hash = current_block_hash
                            anchor_kind = "genesis"
                            node.logger.debug("Fork verify anchor genesis=%s", _hex(anchor_hash))

            current_block = previous_block
            prev_hash = getattr(previous_block, "previous_block_hash", ZERO32) or ZERO32
            if prev_hash == ZERO32:
                self.reached_genesis = True
                break
            current_block_hash = prev_hash

        if current_block is not None and not anchor_validated:
            previous_block: Optional[Block] = None
            prev_hash = getattr(current_block, "previous_block_hash", ZERO32) or ZERO32
            if prev_hash not in (None, ZERO32, b""):
                try:
                    previous_block = get_block_from_storage(astreum_node=node, block_hash=prev_hash)
                except Exception:
                    node.logger.debug("Fork verify failed loading previous block=%s", _hex(prev_hash))
                    previous_block = None
            current_block.previous_block = previous_block
            if not verify_block_head(node, current_block):
                self.malicious_block_hash = (
                    current_block.expr_id
                    or current_block.body_hash
                    or current_block.previous_block_hash
                    or self.head
                )
                node.logger.warning("Fork verify failed header block=%s previous_block=%s", _hex(current_block.expr_id), _hex(previous_block.expr_id) if previous_block else None)
                return False
            if not current_block.expr_id:
                self.malicious_block_hash = (
                    current_block.body_hash
                    or current_block.previous_block_hash
                    or self.head
                )
                node.logger.warning("Fork verify failed missing hash block=%s", _hex(current_block.body_hash))
                return False
            if anchor_hash is None:
                anchor_hash = current_block.expr_id
                anchor_kind = "genesis"
                self.reached_genesis = True
                node.logger.debug("Fork verify anchor genesis=%s", _hex(anchor_hash))
            if current_block.expr_id == anchor_hash:
                anchor_validated = True

        if anchor_hash is None or not anchor_validated:
            node.logger.warning("Fork verify failed anchor validated=%s anchor=%s", anchor_validated, _hex(anchor_hash))
            return False

        node.logger.debug("Fork verify heavy pass head=%s anchor=%s", _hex(self.head), _hex(anchor_hash))
        config = node.config or {}
        current_validator = config.get("validation_public_key_bytes")

        def skip_slow_pass(block: Block) -> bool:
            """Check if slow pass can be skipped.

            Args:
                block: The block to check.

            Returns:
                True if can skip, False otherwise.
            """
            if not current_validator:
                return False
            return (
                getattr(block, "validator_public_key_bytes", None) == current_validator
            )

        heavy_cursor = self.head
        heavy_pending: Optional[Block] = None
        heavy_seen: Set[bytes] = set()
        heavy_anchor_verified = False
        while heavy_cursor and heavy_cursor not in heavy_seen:
            heavy_seen.add(heavy_cursor)
            if heavy_cursor == self.root:
                heavy_anchor_verified = True
                node.logger.debug("Fork verify heavy reached root=%s", _hex(self.root))
                break
            try:
                blk = get_block_from_storage(astreum_node=node, block_hash=heavy_cursor)
            except Exception:
                self.malicious_block_hash = (
                    heavy_pending.expr_id if heavy_pending else heavy_cursor
                )
                node.logger.warning("Fork verify failed heavy load block=%s pending=%s", _hex(heavy_cursor), _hex(heavy_pending.expr_id if heavy_pending else None))
                return False

            if heavy_pending is not None:
                heavy_pending.previous_block = blk
                if skip_slow_pass(heavy_pending):
                    node.logger.debug("Fork verify skipping slow pass for self-validated block=%s", _hex(heavy_pending.expr_id))
                else:
                    verified, reason = verify_block_transactions(node, heavy_pending)
                    if not verified:
                        self.malicious_block_hash = (
                            heavy_pending.expr_id
                            or heavy_pending.previous_block_hash
                            or heavy_cursor
                        )
                        node.logger.warning(
                            "Block verification failed block=%s reason=%s",
                            _hex(heavy_pending.expr_id),
                            reason,
                        )
                        return False
                if heavy_pending.expr_id == anchor_hash:
                    heavy_anchor_verified = True
                    node.logger.debug("Fork verify heavy reached anchor=%s", _hex(anchor_hash))
                    break

            prev_hash = getattr(blk, "previous_block_hash", ZERO32) or ZERO32
            heavy_pending = blk
            if prev_hash == ZERO32:
                break
            heavy_cursor = prev_hash

        if not heavy_anchor_verified and heavy_pending is not None:
            if heavy_pending.expr_id == anchor_hash:
                heavy_pending.previous_block = None
                if skip_slow_pass(heavy_pending):
                    node.logger.debug("Fork verify skipping slow pass for self-validated anchor=%s", _hex(heavy_pending.expr_id))
                else:
                    verified, reason = verify_block_transactions(node, heavy_pending)
                    if not verified:
                        self.malicious_block_hash = (
                            heavy_pending.expr_id
                            or heavy_pending.previous_block_hash
                            or self.head
                        )
                        node.logger.warning(
                            "Block verification failed block=%s reason=%s",
                            _hex(heavy_pending.expr_id),
                            reason,
                        )
                        return False
                heavy_anchor_verified = True

        if not heavy_anchor_verified:
            node.logger.warning("Fork verify failed heavy anchor verified=%s anchor=%s", heavy_anchor_verified, _hex(anchor_hash))
            return False

        if anchor_kind == "fork_head":
            ref = node.forks.get(anchor_hash)
            chain_anchor = ref.chain_fork_position if ref else anchor_hash
            base_root = ref.root if ref and ref.root else anchor_hash
            self.verified_up_to = anchor_hash
            self.chain_fork_position = chain_anchor or anchor_hash
            self.root = base_root
            self.malicious_block_hash = None
            node.forks[self.head] = self
            node.logger.debug("Fork verify committed fork_head head=%s anchor=%s", _hex(self.head), _hex(anchor_hash))
            return True

        if anchor_kind == "intersection":
            base_root = anchor_hash
            existing = node.forks.get(intersection_fork_head) if intersection_fork_head else None
            if existing and existing.root:
                base_root = existing.root

            base_fork = node.forks.get(anchor_hash)
            if base_fork is None:
                base_fork = Fork(head=anchor_hash)
            base_fork.root = base_root
            base_fork.chain_fork_position = anchor_hash
            base_fork.verified_up_to = anchor_hash

            if existing is not None:
                existing.chain_fork_position = anchor_hash
                existing.verified_up_to = anchor_hash
                existing.root = base_root
                node.forks[existing.head] = existing

            self.chain_fork_position = anchor_hash
            self.verified_up_to = anchor_hash
            self.root = base_root
            self.malicious_block_hash = None

            node.forks[base_fork.head] = base_fork
            node.forks[self.head] = self
            node.logger.debug("Fork verify committed intersection head=%s anchor=%s", _hex(self.head), _hex(anchor_hash))
            return True

        if anchor_kind == "genesis":
            self.verified_up_to = anchor_hash
            self.chain_fork_position = anchor_hash
            self.root = anchor_hash
            self.malicious_block_hash = None
            node.forks[self.head] = self
            node.logger.debug("Fork verify committed genesis head=%s anchor=%s", _hex(self.head), _hex(anchor_hash))
            return True

        return False
