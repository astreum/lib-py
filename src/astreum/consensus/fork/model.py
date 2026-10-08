from __future__ import annotations

from typing import Any, Optional, Set

from astreum.expression import ZERO32


class Fork:
    """A branch head within a Chain (same root).

    - head: current tip block id (bytes)
    - peers: identifiers (e.g., peer pubkey objects) following this head
    - header_verified_up_to: optional
    - transaction_verified_up_to: optional
    - malicious_block_hash: optional
    - root: genesis block or branching position
    """
    def __init__(
        self,
        head: bytes,
        root: bytes = ZERO32,
        header_verified_up_to: Optional[bytes] = None,
        transactions_verified_up_to: Optional[bytes] = None,
        malicious_block_hash: Optional[bytes] = None,
    ) -> None:
        self.head: bytes = head
        self.root: bytes = root
        self.header_verified_up_to = header_verified_up_to
        self.transactions_verified_up_to = transactions_verified_up_to
        self.malicious_block_hash = malicious_block_hash
        self.peers: Set[Any] = set()

    def add_peer(self, peer_id: Any) -> None:

        Args:
            peer_id (Any): Description.

        self.peers.add(peer_id)

    def remove_peer(self, peer_id: Any) -> None:

        Args:
            peer_id (Any): Description.

        self.peers.discard(peer_id)

    def to_bytes(self) -> bytes:
        """Serialize fork identity and verification markers."""

        Args:
            None.

        header_flag = b"\x01" if self.header_verified_up_to is not None else b"\x00"
        tx_flag = (
            b"\x01" if self.transactions_verified_up_to is not None else b"\x00"
        )
        header = (
            self.header_verified_up_to
            if self.header_verified_up_to is not None
            else ZERO32
        )
        tx = (
            self.transactions_verified_up_to
            if self.transactions_verified_up_to is not None
            else ZERO32
        )
        malicious_flag = (
            b"\x01" if self.malicious_block_hash is not None else b"\x00"
        )
        malicious = (
            self.malicious_block_hash
            if self.malicious_block_hash is not None
            else ZERO32
        )
        return b"".join(
            [self.head, self.root, header_flag, header, tx_flag, tx, malicious_flag, malicious]
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "Fork":
        """Deserialize fork identity and verification markers."""

        Args:
            payload (bytes): Description.

        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("fork bytes must be bytes")
        block_size = len(ZERO32)
        expected_len = (
            block_size
            + block_size
            + 1
            + block_size
            + 1
            + block_size
            + 1
            + block_size
        )
        if len(payload) != expected_len:
            raise ValueError("fork bytes length mismatch")
        offset = 0
        head = payload[offset : offset + block_size]
        offset += block_size
        root = payload[offset : offset + block_size]
        offset += block_size
        header_flag = payload[offset]
        offset += 1
        header = payload[offset : offset + block_size]
        offset += block_size
        tx_flag = payload[offset]
        offset += 1
        tx = payload[offset : offset + block_size]
        offset += block_size
        malicious_flag = payload[offset]
        offset += 1
        malicious = payload[offset : offset + block_size]

        header_verified_up_to = header if header_flag else None
        transactions_verified_up_to = tx if tx_flag else None
        malicious_block_hash = malicious if malicious_flag else None
        return cls(
            head=head,
            root=root,
            header_verified_up_to=header_verified_up_to,
            transactions_verified_up_to=transactions_verified_up_to,
            malicious_block_hash=malicious_block_hash,
        )
