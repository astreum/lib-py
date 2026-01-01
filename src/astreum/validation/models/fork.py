from __future__ import annotations

from typing import Optional, Set, Any
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .block import Block
from ...storage.models.atom import ZERO32


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

    def verify(self, node: Any) -> bool:
        """Verify this fork using the node to manage fork splits/joins."""
        if node is None:
            raise ValueError("node required for fork validation")

        logger = getattr(node, "logger", None)

        def _hex(value: Optional[bytes]) -> str:
            if isinstance(value, (bytes, bytearray)):
                return value.hex()
            return str(value)

        def _log_debug(message: str, *args: object) -> None:
            if logger:
                logger.debug(message, *args)

        def _log_warning(message: str, *args: object) -> None:
            if logger:
                logger.warning(message, *args)

        _log_debug("Fork verify start head=%s", _hex(self.head))

        visited_set: Set[bytes] = set()
        anchor_hash: Optional[bytes] = None
        anchor_kind: Optional[str] = None
        intersection_fork_head: Optional[bytes] = None
        anchor_validated = False

        def validate_header(child: Block, parent: Optional[Block]) -> bool:
            """
            Lightweight/header validation without tx/receipt/account checks.

            The caller supplies the parent block (or None for genesis) so we can
            verify linkage, height, timestamps, and difficulty in a single pass.
            """
            is_genesis = parent is None or (child.previous_block_hash or ZERO32) == ZERO32

            node_chain = getattr(node, "chain", None)
            if node_chain is not None and child.chain_id != node_chain:
                _log_debug(
                    "Header verify failed chain_id=%s expected=%s block=%s",
                    child.chain_id,
                    node_chain,
                    _hex(child.atom_hash),
                )
                return False

            # Basic field presence
            if child.timestamp is None:
                _log_debug(
                    "Header verify failed missing timestamp block=%s",
                    _hex(child.atom_hash),
                )
                return False
            if not is_genesis:
                if not child.body_hash or not child.signature or not child.validator_public_key_bytes:
                    _log_debug(
                        "Header verify failed missing body/signature/validator block=%s",
                        _hex(child.atom_hash),
                    )
                    return False

            # Linkage rules
            if is_genesis:
                if (child.previous_block_hash or ZERO32) != ZERO32:
                    _log_debug(
                        "Header verify failed genesis prev_hash=%s block=%s",
                        _hex(child.previous_block_hash),
                        _hex(child.atom_hash),
                    )
                    return False
                if child.number not in (0,):
                    _log_debug(
                        "Header verify failed genesis number=%s block=%s",
                        child.number,
                        _hex(child.atom_hash),
                    )
                    return False
            else:
                parent_hash = parent.atom_hash or ZERO32
                if (child.previous_block_hash or ZERO32) != parent_hash:
                    _log_debug(
                        "Header verify failed prev hash mismatch block=%s prev=%s expected=%s",
                        _hex(child.atom_hash),
                        _hex(child.previous_block_hash),
                        _hex(parent_hash),
                    )
                    return False
                expected_number = (parent.number or 0) + 1
                if child.number != expected_number:
                    _log_debug(
                        "Header verify failed number mismatch block=%s number=%s expected=%s",
                        _hex(child.atom_hash),
                        child.number,
                        expected_number,
                    )
                    return False

                parent_ts = parent.timestamp
                if parent_ts is not None and int(child.timestamp) < int(parent_ts) + 1:
                    _log_debug(
                        "Header verify failed timestamp block=%s ts=%s parent_ts=%s",
                        _hex(child.atom_hash),
                        child.timestamp,
                        parent_ts,
                    )
                    return False

                # Signature over body hash
                try:
                    pub = Ed25519PublicKey.from_public_bytes(
                        bytes(child.validator_public_key_bytes)
                    )
                    pub.verify(child.signature, child.body_hash)  # type: ignore[arg-type]
                except InvalidSignature:
                    _log_debug(
                        "Header verify failed signature block=%s",
                        _hex(child.atom_hash),
                    )
                    return False
                except Exception:
                    _log_debug(
                        "Header verify failed signature error block=%s",
                        _hex(child.atom_hash),
                    )
                    return False

                # Difficulty and PoW
                expected_diff = Block.calculate_delay_difficulty(
                    previous_timestamp=parent.timestamp,
                    current_timestamp=child.timestamp,
                    previous_difficulty=parent.delay_difficulty,
                )
                if child.delay_difficulty is None or int(child.delay_difficulty) != int(
                    expected_diff
                ):
                    _log_debug(
                        "Header verify failed difficulty block=%s diff=%s expected=%s",
                        _hex(child.atom_hash),
                        child.delay_difficulty,
                        expected_diff,
                    )
                    return False

                required_work = max(1, int(parent.delay_difficulty or 1))
                block_hash = child.atom_hash or b""
                if not block_hash:
                    _log_debug(
                        "Header verify failed missing hash block=%s",
                        _hex(child.atom_hash),
                    )
                    return False
                if Block._leading_zero_bits(block_hash) < required_work:
                    _log_debug(
                        "Header verify failed pow block=%s zeros=%s required=%s",
                        _hex(child.atom_hash),
                        Block._leading_zero_bits(block_hash),
                        required_work,
                    )
                    return False

            return True

        def is_on_other_fork_path(target_hash: bytes) -> Optional[bytes]:
            """Return the head of a fork whose ancestry includes target_hash."""
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
                        blk = Block.from_atom(node, blk_hash)
                    except Exception:
                        _log_debug(
                            "Fork path lookup failed loading block=%s",
                            _hex(blk_hash),
                        )
                        blk = None
                    if blk is None:
                        break
                    prev = getattr(blk, "previous_block_hash", ZERO32) or ZERO32
                    if prev == ZERO32:
                        break
                    blk_hash = prev
            return None

        cursor = self.head
        pending_child: Optional[Block] = None
        while cursor and cursor not in visited_set:
            try:
                blk = Block.from_atom(node, cursor)
            except Exception:
                _log_debug("Fork verify failed loading block=%s", _hex(cursor))
                blk = None
            if blk is None:
                self.malicious_block_hash = (
                    pending_child.atom_hash if pending_child else cursor
                )
                _log_warning(
                    "Fork verify failed missing block=%s pending=%s",
                    _hex(cursor),
                    _hex(pending_child.atom_hash) if pending_child else None,
                )
                return False

            if pending_child is not None:
                if not validate_header(pending_child, blk):
                    self.malicious_block_hash = (
                        pending_child.atom_hash
                        or pending_child.body_hash
                        or pending_child.previous_block_hash
                        or cursor
                    )
                    _log_warning(
                        "Fork verify failed header block=%s parent=%s",
                        _hex(pending_child.atom_hash),
                        _hex(blk.atom_hash),
                    )
                    return False
                if not pending_child.atom_hash:
                    self.malicious_block_hash = (
                        pending_child.body_hash
                        or pending_child.previous_block_hash
                        or cursor
                    )
                    _log_warning(
                        "Fork verify failed missing hash block=%s",
                        _hex(pending_child.body_hash),
                    )
                    return False
                if anchor_hash is not None and pending_child.atom_hash == anchor_hash:
                    anchor_validated = True
                    _log_debug(
                        "Fork verify reached anchor=%s kind=%s",
                        _hex(anchor_hash),
                        anchor_kind,
                    )
                    break

            visited_set.add(cursor)

            if anchor_hash is None:
                if cursor in node.forks and cursor != self.head:
                    anchor_hash = cursor
                    anchor_kind = "fork_head"
                    _log_debug(
                        "Fork verify anchor fork_head=%s",
                        _hex(anchor_hash),
                    )
                else:
                    other_head = is_on_other_fork_path(cursor)
                    if other_head:
                        anchor_hash = cursor
                        anchor_kind = "intersection"
                        intersection_fork_head = other_head
                        _log_debug(
                            "Fork verify anchor intersection=%s other_head=%s",
                            _hex(anchor_hash),
                            _hex(other_head),
                        )
                    else:
                        prev_hash = getattr(blk, "previous_block_hash", ZERO32) or ZERO32
                        if prev_hash == ZERO32:
                            anchor_hash = cursor
                            anchor_kind = "genesis"
                            _log_debug(
                                "Fork verify anchor genesis=%s",
                                _hex(anchor_hash),
                            )

            pending_child = blk
            prev_hash = getattr(blk, "previous_block_hash", ZERO32) or ZERO32
            if prev_hash == ZERO32:
                break
            cursor = prev_hash

        if pending_child is not None and not anchor_validated:
            parent_blk: Optional[Block] = None
            prev_hash = getattr(pending_child, "previous_block_hash", ZERO32) or ZERO32
            if prev_hash not in (None, ZERO32, b""):
                try:
                    parent_blk = Block.from_atom(node, prev_hash)
                except Exception:
                    _log_debug(
                        "Fork verify failed loading parent block=%s",
                        _hex(prev_hash),
                    )
                    parent_blk = None
            if not validate_header(pending_child, parent_blk):
                self.malicious_block_hash = (
                    pending_child.atom_hash
                    or pending_child.body_hash
                    or pending_child.previous_block_hash
                    or self.head
                )
                _log_warning(
                    "Fork verify failed header block=%s parent=%s",
                    _hex(pending_child.atom_hash),
                    _hex(parent_blk.atom_hash) if parent_blk else None,
                )
                return False
            if not pending_child.atom_hash:
                self.malicious_block_hash = (
                    pending_child.body_hash
                    or pending_child.previous_block_hash
                    or self.head
                )
                _log_warning(
                    "Fork verify failed missing hash block=%s",
                    _hex(pending_child.body_hash),
                )
                return False
            if anchor_hash is None:
                anchor_hash = pending_child.atom_hash
                anchor_kind = "genesis"
                _log_debug(
                    "Fork verify anchor genesis=%s",
                    _hex(anchor_hash),
                )
            if pending_child.atom_hash == anchor_hash:
                anchor_validated = True

        if anchor_hash is None or not anchor_validated:
            _log_warning(
                "Fork verify failed anchor validated=%s anchor=%s",
                anchor_validated,
                _hex(anchor_hash),
            )
            return False

        _log_debug(
            "Fork verify heavy pass head=%s anchor=%s",
            _hex(self.head),
            _hex(anchor_hash),
        )
        heavy_cursor = self.head
        heavy_pending: Optional[Block] = None
        heavy_seen: Set[bytes] = set()
        heavy_anchor_verified = False
        while heavy_cursor and heavy_cursor not in heavy_seen:
            heavy_seen.add(heavy_cursor)
            try:
                blk = Block.from_atom(node, heavy_cursor)
            except Exception:
                self.malicious_block_hash = (
                    heavy_pending.atom_hash if heavy_pending else heavy_cursor
                )
                _log_warning(
                    "Fork verify failed heavy load block=%s pending=%s",
                    _hex(heavy_cursor),
                    _hex(heavy_pending.atom_hash) if heavy_pending else None,
                )
                return False

            if heavy_pending is not None:
                heavy_pending.previous_block = blk
                if not heavy_pending.verify(node):
                    self.malicious_block_hash = (
                        heavy_pending.atom_hash
                        or heavy_pending.previous_block_hash
                        or heavy_cursor
                    )
                    _log_warning(
                        "Fork verify failed heavy block=%s parent=%s",
                        _hex(heavy_pending.atom_hash),
                        _hex(blk.atom_hash),
                    )
                    return False
                if heavy_pending.atom_hash == anchor_hash:
                    heavy_anchor_verified = True
                    _log_debug(
                        "Fork verify heavy reached anchor=%s",
                        _hex(anchor_hash),
                    )
                    break

            prev_hash = getattr(blk, "previous_block_hash", ZERO32) or ZERO32
            heavy_pending = blk
            if prev_hash == ZERO32:
                break
            heavy_cursor = prev_hash

        if not heavy_anchor_verified and heavy_pending is not None:
            if heavy_pending.atom_hash == anchor_hash:
                heavy_pending.previous_block = None
                if not heavy_pending.verify(node):
                    self.malicious_block_hash = (
                        heavy_pending.atom_hash
                        or heavy_pending.previous_block_hash
                        or self.head
                    )
                    _log_warning(
                        "Fork verify failed heavy anchor block=%s",
                        _hex(heavy_pending.atom_hash),
                    )
                    return False
                heavy_anchor_verified = True

        if not heavy_anchor_verified:
            _log_warning(
                "Fork verify failed heavy anchor verified=%s anchor=%s",
                heavy_anchor_verified,
                _hex(anchor_hash),
            )
            return False

        # Commit staged fork edits
        if anchor_kind == "fork_head":
            ref = node.forks.get(anchor_hash)
            chain_anchor = ref.chain_fork_position if ref else anchor_hash
            base_root = ref.root if ref and ref.root else anchor_hash
            self.validated_upto = anchor_hash
            self.chain_fork_position = chain_anchor or anchor_hash
            self.root = base_root
            self.malicious_block_hash = None
            node.forks[self.head] = self
            _log_debug(
                "Fork verify committed fork_head head=%s anchor=%s",
                _hex(self.head),
                _hex(anchor_hash),
            )
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
            base_fork.validated_upto = anchor_hash

            if existing is not None:
                existing.chain_fork_position = anchor_hash
                existing.validated_upto = anchor_hash
                existing.root = base_root
                node.forks[existing.head] = existing

            self.chain_fork_position = anchor_hash
            self.validated_upto = anchor_hash
            self.root = base_root
            self.malicious_block_hash = None

            node.forks[base_fork.head] = base_fork
            node.forks[self.head] = self
            _log_debug(
                "Fork verify committed intersection head=%s anchor=%s",
                _hex(self.head),
                _hex(anchor_hash),
            )
            return True

        if anchor_kind == "genesis":
            self.validated_upto = anchor_hash
            self.chain_fork_position = anchor_hash
            self.root = anchor_hash
            self.malicious_block_hash = None
            node.forks[self.head] = self
            _log_debug(
                "Fork verify committed genesis head=%s anchor=%s",
                _hex(self.head),
                _hex(anchor_hash),
            )
            return True

        return False
