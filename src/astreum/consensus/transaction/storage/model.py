from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from astreum.expression import Expr, NIL, resolve_list_exprs, link, int_
from astreum.expression import ZERO32, get_expr_tag, get_expr_value
from astreum.storage.exprs import get_expr



@dataclass
class StorageRecord:
    creation_block_hash: bytes = b""
    last_payment_block_hash: bytes = b""
    last_payment_height: int = 0
    last_payment_winner: bytes = b""
    new_size: int = 0
    new_count: int = 0
    mint: bool = False
    _expr: Optional[Expr] = field(default=None, repr=False, compare=False)

    def to_expr(self) -> Expr:
        if self._expr is not None:
            return self._expr
        # Permanent core (innermost to outermost)
        tail: Expr = link(int_(self.new_size), NIL)
        tail = link(int_(self.new_count), tail)
        tail = link(Expr("link", head_hash=self.creation_block_hash), tail)
        # Transient wrapper (rewritten each payment — 5 new nodes)
        tail = link(int_(1 if self.mint else 0), tail)
        tail = link(Expr("link", head_hash=self.last_payment_winner), tail)
        tail = link(int_(self.last_payment_height), tail)
        tail = link(Expr("link", head_hash=self.last_payment_block_hash), tail)
        return tail

    def expr(self) -> Expr:
        if self._expr is not None:
            return self._expr
        self._expr = self.to_expr()
        return self._expr

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
    def from_storage(cls, node: Any, expr_id: bytes) -> StorageRecord | None:
        header = get_expr(node, expr_id)
        if header is None or not header._tag == "link":
            return None
        nodes, missed = resolve_list_exprs(node, header)
        if missed:
            return None
        if len(nodes) not in (6, 7):
            return None
        # nodes[0-2]: Link hash pointers (block_hash, winner, creation)
        # nodes[3]: Int/0/1 mint flag (added in 7-node format)
        # nodes[4]: Link (creation_block_hash)
        # nodes[5-6]: Int (count, size)
        mint_node_idx = 3
        winner_node_idx = 2
        creation_node_idx = 4 if len(nodes) == 7 else 3
        count_node_idx = 5 if len(nodes) == 7 else 4
        size_node_idx = 6 if len(nodes) == 7 else 5
        if not get_expr_tag(nodes[mint_node_idx], node) == "int":
            return None
        if not all(get_expr_tag(n, node) == "link" for n in [nodes[0], nodes[winner_node_idx], nodes[creation_node_idx]]):
            return None
        if not all(get_expr_tag(n, node) == "int" for n in [nodes[1], nodes[count_node_idx], nodes[size_node_idx]]):
            return None
        obj = cls(
            last_payment_block_hash=cls._extract_hash(nodes[0]),
            last_payment_height=get_expr_value(nodes[1], node),
            mint=bool(get_expr_value(nodes[mint_node_idx], node)),
            last_payment_winner=cls._extract_hash(nodes[winner_node_idx]),
            creation_block_hash=cls._extract_hash(nodes[creation_node_idx]),
            new_count=get_expr_value(nodes[count_node_idx], node),
            new_size=get_expr_value(nodes[size_node_idx], node),
        )
        obj._expr = header
        return obj


@dataclass
class StorageSlot:
    storage_id: bytes
    sequence: int
    _expr: Optional[Expr] = field(default=None, repr=False, compare=False)

    def to_expr(self) -> Expr:
        if self._expr is not None:
            return self._expr
        return Expr("link",
            head_hash=self.storage_id,
            tail=int_(self.sequence),
        )

    def expr(self) -> Expr:
        if self._expr is not None:
            return self._expr
        self._expr = self.to_expr()
        return self._expr

    @classmethod
    def from_storage(cls, node: Any, expr_id: bytes) -> StorageSlot | None:
        expr = get_expr(node, expr_id)
        if not expr._tag == "link":
            return None
        if expr._head_hash is None:
            return None
        tail = expr._tail
        if not get_expr_tag(tail, node) == "int":
            return None
        obj = cls(
            storage_id=expr._head_hash,
            sequence=get_expr_value(tail, node),
        )
        obj._expr = expr
        return obj
