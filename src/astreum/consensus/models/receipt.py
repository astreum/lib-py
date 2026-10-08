from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from astreum.expression import Expr, NIL, get_expr_tag, get_expr_value, resolve_list_exprs, link, int_, symbol
from astreum.expression import ZERO32
from astreum.storage.exprs import get_expr_list

STATUS_SUCCESS = 0
STATUS_FAILED = 1


def _read_body(node, body, contact, scope):
    """The body's six values and the transaction hash its last cell names.

    The last cell holds the transaction by hash and is never fetched: the
    receipt names the transaction, it does not need its body.

    Args:
        node: The storage node.
        body: The body expression.
        contact: Optional contact.
        scope: Scope dictionary.

    Returns:
        Tuple of (items, tx_hash, missed).
    """
    from astreum.storage.exprs import get_expr

    items: list[Expr] = []
    missed: list[bytes] = []
    tx_hash = None
    cell = body
    while cell is not None and cell.base == "link":
        last = cell.tail is None and cell.tail_hash in (None, ZERO32)
        if last:
            if cell.head_hash is not None:
                tx_hash = cell.head_hash
            elif cell.head is not None:
                tx_hash = cell.head.hash()
            break
        head = cell.head
        if head is None and cell.head_hash is not None:
            head = get_expr(node, cell.head_hash, contact=contact, **scope)
            if head is None:
                missed.append(cell.head_hash)
                break
            cell.head, cell.head_hash = head, None
        items.append(head)
        if cell.tail is None:
            nxt = get_expr(node, cell.tail_hash, contact=contact, **scope)
            if nxt is None:
                missed.append(cell.tail_hash)
                break
            cell.tail, cell.tail_hash = nxt, None
        cell = cell.tail
    return items, tx_hash, missed


class Receipt:
    def __init__(
        self,
        transaction_hash: bytes,
        transaction_fee: int,
        storage_fee: int,
        data_fee: int,
        execution_fee: int,
        status: int,
        logs_hash: bytes = ZERO32,
    ) -> None:
        self.transaction_hash = transaction_hash
        self.transaction_fee = transaction_fee
        self.storage_fee = storage_fee
        self.data_fee = data_fee
        self.execution_fee = execution_fee
        self.logs_hash = logs_hash
        self.status = status
        self.expr_id = ZERO32
        self._expr: Optional["Expr"] = None

    @property
    def total_fee(self) -> int:
        """Get the total fee.

        Args:
            None.

        Returns:
            Sum of transaction, data, and storage fees.
        """
        # transaction_fee holds the execution fee: counting it again would
        # charge a call twice.
        return self.transaction_fee + self.data_fee + self.storage_fee

    def to_expr(self) -> "Expr":
        """Convert receipt to expression.

        Args:
            None.

        Returns:
            The expression representation.
        """
        body: Expr = Expr("link", head_hash=self.transaction_hash)
        body = link(int_(self.transaction_fee), body)
        body = link(int_(self.storage_fee), body)
        body = link(int_(self.status), body)
        body = link(Expr("link", head_hash=self.logs_hash), body)
        body = link(int_(self.execution_fee), body)
        body = link(int_(self.data_fee), body)
        return link(
            link(body, NIL),
            symbol("receipt"))

    def expr(self) -> "Expr":
        """Get the cached expression or compute it.

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
    def from_storage(
        cls, node: Any, receipt_id: bytes, contact=None, block_hash=None
    ) -> "Receipt":
        """Load a receipt; *block_hash* is the block whose provider holds it."""
        scope = {} if block_hash is None else {"block_hash": block_hash}
        header = get_expr_list(node, receipt_id, contact=contact, **scope)
        if header is None:
            raise ValueError("unable to load receipt from storage")
        if not header._tag == "link":
            raise ValueError("receipt header must be a Link")
        if header._tail is None or header._tail._tag != "symbol" or header._tail.value != "receipt":
            raise ValueError(
                f"invalid receipt type tag (got {header._tail!r})"
            )

        inner = header._head
        if inner is None and header._head_hash is not None:
            from astreum.storage.exprs import get_expr

            inner = get_expr(node, header._head_hash, contact=contact, **scope)
            header._head = inner
        if inner is None or inner._tag != "link":
            raise ValueError("receipt inner header must be a Link")

        inner_nodes, missed = resolve_list_exprs(node, inner, contact=contact, **scope)
        if missed:
            raise ValueError(
                f"unable to resolve receipt header (missed={[h.hex()[:8] for h in missed]})"
            )
        if len(inner_nodes) != 1:
            raise ValueError(
                f"malformed receipt header length (got={len(inner_nodes)}, expected=1)"
            )

        body = inner_nodes[0]
        if not body._tag == "link":
            raise ValueError("receipt body must be a Link chain")

        body_nodes, tx_hash, missed = _read_body(node, body, contact, scope)
        if missed:
            raise ValueError(
                f"unable to resolve receipt body (missed={[h.hex()[:8] for h in missed]})"
            )
        if tx_hash is None or len(body_nodes) != 6:
            raise ValueError(
                f"malformed receipt body length (got={len(body_nodes)}, expected=7)"
            )

        (
            data_fee_node,
            execution_fee_node,
            logs_node,
            status_node,
            storage_fee_node,
            transaction_fee_node,
        ) = body_nodes

        if not get_expr_tag(data_fee_node, node, contact, block_hash) == "int":
            raise ValueError("expected Int for data_fee")
        if not get_expr_tag(execution_fee_node, node, contact, block_hash) == "int":
            raise ValueError("expected Int for execution_fee")
        if not logs_node._tag == "link":
            raise ValueError("expected Link for logs_hash")
        # No logs: the hash is ZERO32, which a stored cell reads back as NIL.
        if logs_node._head_hash is not None:
            logs_hash = logs_node._head_hash
        elif logs_node._head is None or logs_node._head is NIL:
            logs_hash = ZERO32
        else:
            logs_hash = logs_node._head.hash()
        if not get_expr_tag(status_node, node, contact, block_hash) == "int":
            raise ValueError("expected Int for status")
        if not get_expr_tag(storage_fee_node, node, contact, block_hash) == "int":
            raise ValueError("expected Int for storage_fee")
        if not get_expr_tag(transaction_fee_node, node, contact, block_hash) == "int":
            raise ValueError("expected Int for transaction_fee")

        status_value = get_expr_value(status_node, node, contact, block_hash)
        if status_value not in (STATUS_SUCCESS, STATUS_FAILED):
            raise ValueError("unsupported receipt status")

        receipt = cls(
            transaction_hash=tx_hash,
            transaction_fee=get_expr_value(transaction_fee_node, node, contact, block_hash),
            storage_fee=get_expr_value(storage_fee_node, node, contact, block_hash),
            data_fee=get_expr_value(data_fee_node, node, contact, block_hash),
            execution_fee=get_expr_value(execution_fee_node, node, contact, block_hash),
            logs_hash=logs_hash,
            status=status_value,
        )
        receipt._expr = header
        receipt.expr_id = receipt_id
        return receipt
