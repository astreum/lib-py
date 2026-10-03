from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from astreum.expression import Expr, NIL, resolve_list_exprs, link, int_, symbol
from astreum.expression import ZERO32
from astreum.storage.exprs import get_expr_list

STATUS_SUCCESS = 0
STATUS_FAILED = 1


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
        mint: int = 0,
    ) -> None:
        self.transaction_hash = transaction_hash
        self.transaction_fee = transaction_fee
        self.storage_fee = storage_fee
        self.data_fee = data_fee
        self.execution_fee = execution_fee
        self.logs_hash = logs_hash
        self.mint = mint
        self.status = status
        self.expr_id = ZERO32
        self._expr: Optional["Expr"] = None

    @property
    def total_fee(self) -> int:
        return self.transaction_fee + self.data_fee + self.execution_fee + self.storage_fee

    def to_expr(self) -> "Expr":
        body: Expr = Expr("link", head_hash=self.transaction_hash)
        body = link(int_(self.transaction_fee), body)
        body = link(int_(self.storage_fee), body)
        body = link(int_(self.status), body)
        body = link(Expr("link", head_hash=self.logs_hash), body)
        body = link(int_(self.execution_fee), body)
        body = link(int_(self.data_fee), body)
        body = link(int_(self.mint), body)
        return link(
            link(body, NIL),
            symbol("receipt"))

    def expr(self) -> "Expr":
        if self._expr is not None:
            return self._expr
        self._expr = self.to_expr()
        return self._expr

    @classmethod
    def from_storage(cls, node: Any, receipt_id: bytes, contact=None) -> "Receipt":
        header = get_expr_list(node, receipt_id, contact=contact)
        if header is None:
            raise ValueError("unable to load receipt from storage")
        if not header._tag == "link":
            raise ValueError("receipt header must be a Link")
        if header._tail is None or header._tail._tag != "symbol" or header._tail.value != "receipt":
            raise ValueError(
                f"invalid receipt type tag (got {header._tail!r})"
            )

        inner = header._head
        if inner is None or inner._tag != "link":
            raise ValueError("receipt inner header must be a Link")

        inner_nodes, missed = resolve_list_exprs(node, inner, contact=contact)
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

        body_nodes, missed = resolve_list_exprs(node, body, contact=contact)
        if missed:
            raise ValueError(
                f"unable to resolve receipt body (missed={[h.hex()[:8] for h in missed]})"
            )
        if len(body_nodes) != 8:
            raise ValueError(
                f"malformed receipt body length (got={len(body_nodes)}, expected=8)"
            )

        (
            mint_node,
            data_fee_node,
            execution_fee_node,
            logs_node,
            status_node,
            storage_fee_node,
            transaction_fee_node,
            tx_hash_node,
        ) = body_nodes

        if not mint_node._tag == "int":
            raise ValueError("expected Int for mint")
        if not data_fee_node._tag == "int":
            raise ValueError("expected Int for data_fee")
        if not execution_fee_node._tag == "int":
            raise ValueError("expected Int for execution_fee")
        if not logs_node._tag == "link" or logs_node._head_hash is None:
            raise ValueError("expected Link with head_hash for logs_hash")
        if not status_node._tag == "int":
            raise ValueError("expected Int for status")
        if not storage_fee_node._tag == "int":
            raise ValueError("expected Int for storage_fee")
        if not transaction_fee_node._tag == "int":
            raise ValueError("expected Int for transaction_fee")
        if not tx_hash_node._tag == "link" or tx_hash_node._head_hash is None:
            raise ValueError("expected Link with head_hash for transaction_hash")

        status_value = status_node.value
        if status_value not in (STATUS_SUCCESS, STATUS_FAILED):
            raise ValueError("unsupported receipt status")

        receipt = cls(
            transaction_hash=tx_hash_node._head_hash,
            transaction_fee=transaction_fee_node.value,
            storage_fee=storage_fee_node.value,
            data_fee=data_fee_node.value,
            execution_fee=execution_fee_node.value,
            logs_hash=logs_node._head_hash,
            status=status_value,
            mint=mint_node.value,
        )
        receipt._expr = header
        receipt.expr_id = receipt_id
        return receipt
