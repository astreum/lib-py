from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from astreum.expression import Expr, NIL, resolve_list_exprs, link, int_
from astreum.expression import ZERO32
from astreum.storage.exprs import get_expr_list
from astreum.expression.helpers import get_expr_builtin_tag


@dataclass
class Channel:
    balance: int
    counter: int
    withdrawal_window: int
    _expr: Optional[Expr] = field(default=None, repr=False, compare=False)

    def to_expr(self) -> Expr:
        """Convert channel to expression.

        Args:
            None.

        Returns:
            The expression representation.
        """
        if self._expr is not None:
            return self._expr
        detail: Expr = link(int_(self.withdrawal_window), NIL)
        detail = link(int_(self.counter), detail)
        detail = link(int_(self.balance), detail)
        return detail

    def expr(self) -> Expr:
        """Get cached expression or compute it.

        Args:
            None.

        Returns:
            The expression representation.
        """
        if self._expr is not None:
            return self._expr
        self._expr = self.to_expr()
        return self._expr

    @classmethod
    def from_storage(cls, node, head_hash: bytes, contact=None) -> Optional["Channel"]:
        """Load a Channel from storage.

        Args:
            node: The node.
            head_hash: The head hash.
            contact: Optional contact.

        Returns:
            The Channel or None.
        """
        if not head_hash or head_hash == ZERO32:
            return None
        header = get_expr_list(node, head_hash, contact=contact)
        if header is None or not get_expr_builtin_tag(header) == "link":
            return None
        nodes, missed = resolve_list_exprs(node, header, contact=contact)
        if missed:
            return None
        if len(nodes) != 3:
            return None
        if not get_expr_builtin_tag(nodes[0]) == "int":
            return None
        if not get_expr_builtin_tag(nodes[1]) == "int":
            return None
        if not get_expr_builtin_tag(nodes[2]) == "int":
            return None
        return cls(
            balance=nodes[0].value,
            counter=nodes[1].value,
            withdrawal_window=nodes[2].value,
        )
