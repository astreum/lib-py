from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from astreum.expression import Expr, NIL, resolve_list_exprs, link, int_, bytes_
from astreum.expression import ZERO32, get_expr_tag, get_expr_value
from astreum.storage.exprs import get_expr



@dataclass
class StorageRecord:
    creation_block_hash: bytes = b""
    last_payment_block_hash: bytes = b""
    last_payment_height: int = 0
    last_payment_winner: bytes = ZERO32
    new_size: int = 0
    new_count: int = 0
    slots_root: bytes = ZERO32
    _expr: Optional[Expr] = field(default=None, repr=False, compare=False)

    def to_expr(self) -> Expr:
        if self._expr is not None:
            return self._expr
        # Permanent core (innermost to outermost)
        tail: Expr = link(int_(self.new_size), NIL)
        tail = link(int_(self.new_count), tail)
        # A bytes value, not a hash link: the root names no stored expr, and
        # the record walk follows every hash link.
        tail = link(bytes_(self.slots_root), tail)
        tail = link(Expr("link", head_hash=self.creation_block_hash), tail)
        # Transient wrapper (rewritten each payment). The winner is an address,
        # also a bytes value.
        tail = link(bytes_(self.last_payment_winner), tail)
        tail = link(int_(self.last_payment_height), tail)
        tail = link(Expr("link", head_hash=self.last_payment_block_hash), tail)
        return tail

    def expr(self) -> Expr:
        if self._expr is not None:
            return self._expr
        self._expr = self.to_expr()
        return self._expr

    def cells(self) -> list[Expr]:
        """The record's own exprs, to store with the block that writes it.

        Only what the record holds: the hashes it names (blocks) are left
        unresolved, so this never fetches and never pulls a block's tree in.
        """
        out: list[Expr] = []
        seen: set[bytes] = set()
        stack = [self.expr()]
        while stack:
            e = stack.pop()
            h = e.hash()
            if h in seen or h == ZERO32:
                continue
            seen.add(h)
            out.append(e)
            if e._tag == "link":
                for child in (e._tail, e._head):
                    if child is not None:
                        stack.append(child)
        return out

    @staticmethod
    def _extract_hash(n: Expr) -> bytes:
        """Extract the stored hash from a node, whether resolved or not."""
        if n._tag == "link":
            if n._head_hash is not None:
                return n._head_hash
            if n._head is not None:
                return n._head.hash()
        return ZERO32

    @classmethod
    def from_storage(
        cls, node: Any, expr_id: bytes, contact=None, block_hash=None
    ) -> StorageRecord | None:
        """*block_hash* is the block whose record holds the record's cells."""
        scope = {} if block_hash is None else {"block_hash": block_hash}
        header = get_expr(node, expr_id, contact=contact, **scope)
        return cls.from_expr(node, header, contact=contact, block_hash=block_hash)

    @classmethod
    def from_expr(
        cls, node: Any, header: Optional[Expr], contact=None, block_hash=None
    ) -> StorageRecord | None:
        """Parse a record header already in hand (the layout lives here)."""
        if header is None or not header._tag == "link":
            return None
        nodes, missed = resolve_list_exprs(node, header, contact=contact, block_hash=block_hash)
        if missed:
            return None
        if len(nodes) != 7:
            return None
        # nodes[0]: Link (last_payment_block_hash)   nodes[1]: Int (height)
        # nodes[2]: Bytes (winner)                   nodes[3]: Link (creation_block_hash)
        # nodes[4]: Bytes (slots_root)               nodes[5]: Int (count)
        # nodes[6]: Int (size)
        winner_node_idx = 2
        creation_node_idx = 3
        root_node_idx = 4
        count_node_idx = 5
        size_node_idx = 6
        if not all(get_expr_tag(n, node, contact, block_hash) == "link" for n in [nodes[0], nodes[creation_node_idx]]):
            return None
        if not all(get_expr_tag(n, node, contact, block_hash) == "bytes" for n in [nodes[winner_node_idx], nodes[root_node_idx]]):
            return None
        if not all(get_expr_tag(n, node, contact, block_hash) == "int" for n in [nodes[1], nodes[count_node_idx], nodes[size_node_idx]]):
            return None
        obj = cls(
            last_payment_block_hash=cls._extract_hash(nodes[0]),
            last_payment_height=get_expr_value(nodes[1], node, contact, block_hash),
            last_payment_winner=get_expr_value(nodes[winner_node_idx], node, contact, block_hash),
            creation_block_hash=cls._extract_hash(nodes[creation_node_idx]),
            slots_root=get_expr_value(nodes[root_node_idx], node, contact, block_hash),
            new_count=get_expr_value(nodes[count_node_idx], node, contact, block_hash),
            new_size=get_expr_value(nodes[size_node_idx], node, contact, block_hash),
        )
        obj._expr = header
        return obj
