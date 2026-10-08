from __future__ import annotations

from typing import Optional, Union

from astreum.expression import Expr


class RadixNode:
    def __init__(
        self,
        key_len: int,
        key: bytes,
        value: Optional[Union[Expr, bytes]],
        child_0: Optional[bytes],
        child_1: Optional[bytes],
        child_0_block: Optional[bytes] = None,
        child_1_block: Optional[bytes] = None,
        value_block: Optional[bytes] = None,
    ):
        self.key_len = key_len
        self.key = key
        self.value = value
        self.child_0 = child_0
        self.child_1 = child_1
        # The block whose record holds the cell each pointer names, when the
        # cell is older than the block that writes this node: the node is
        # written with a ``(block_id expr_id ref)`` in place of the bare hash.
        # None for a cell that is new (a plain hash). Loaded from the node's
        # encoding: a ref names its block, a plain hash is in the block that
        # holds the node. See docs/plans/2026-10-08-refs-in-tries.md.
        self.child_0_block = child_0_block
        self.child_1_block = child_1_block
        self.value_block = value_block
        self._hash: Optional[bytes] = None
        self._expr: Optional["Expr"] = None
