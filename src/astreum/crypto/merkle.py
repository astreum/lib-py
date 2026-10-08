from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from blake3 import blake3

from astreum.expression import ZERO32

HASH_LEN = 32


@dataclass
class MerkleTree:
    root: bytes
    count: int
    levels: Optional[List[List[bytes]]] = None


def _leaf(value: bytes) -> bytes:
    return blake3(b"\x00" + value).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return blake3(b"\x01" + left + right).digest()


def create_merkle_tree(values: List[bytes]) -> MerkleTree:
    """Build a Merkle tree over an ordered list of 32-byte values.

    Args:
        values: List of 32-byte values.

    Returns:
        MerkleTree: Description of the return value.
    """
    for v in values:
        if not isinstance(v, (bytes, bytearray)) or len(v) != HASH_LEN:
            raise ValueError("merkle values must be 32 bytes")

    if not values:
        return MerkleTree(root=ZERO32, count=0, levels=[])

    level = [_leaf(bytes(v)) for v in values]
    levels = [level]
    while len(level) > 1:
        nxt = [
            _node(level[j], level[j + 1]) if j + 1 < len(level) else level[j]
            for j in range(0, len(level), 2)
        ]
        levels.append(nxt)
        level = nxt
    return MerkleTree(root=level[0], count=len(values), levels=levels)


def generate_merkle_tree_proof(merkle_tree: MerkleTree, index: int) -> List[bytes]:
    """Generate sibling hashes for index, leaf to root.

    Args:
        merkle_tree: Merkle tree.
        index: Index of the leaf.

    Returns:
        List[bytes]: Description of the return value.
    """
    if merkle_tree.levels is None:
        raise ValueError("merkle tree has no levels")
    if not 0 <= index < merkle_tree.count:
        raise ValueError("index out of range")

    proof: List[bytes] = []
    i = index
    for level in merkle_tree.levels[:-1]:
        if (i ^ 1) < len(level):
            proof.append(level[i ^ 1])
        i >>= 1
    return proof


def verify_merkle_tree_proof(
    merkle_tree: MerkleTree,
    index: int,
    value: bytes,
    proof: List[bytes],
) -> bool:
    """
    Verify value sits at index under merkle_tree.root.

    Args:
        merkle_tree: Merkle tree.
        index: Index of the leaf.
        value: Value to verify.
        proof: List of sibling hashes.

    Returns:
        bool: True if the proof is valid, False otherwise.
    """
    n = merkle_tree.count
    if not isinstance(index, int) or not 0 <= index < n:
        return False
    if not isinstance(value, (bytes, bytearray)) or len(value) != HASH_LEN:
        return False
    for s in proof:
        if not isinstance(s, (bytes, bytearray)) or len(s) != HASH_LEN:
            return False

    fn, sn = index, n - 1
    r = _leaf(bytes(value))
    used = 0
    while sn > 0:
        if fn & 1:
            if used >= len(proof):
                return False
            r = _node(bytes(proof[used]), r)
            used += 1
        elif fn < sn:
            if used >= len(proof):
                return False
            r = _node(r, bytes(proof[used]))
            used += 1
        fn >>= 1
        sn >>= 1
    return used == len(proof) and r == merkle_tree.root
