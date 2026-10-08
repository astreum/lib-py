from __future__ import annotations

from typing import TYPE_CHECKING, Any

from astreum.expression import Expr, get_expr_tag, get_expr_value, resolve_inner_exprs, resolve_list_exprs
from astreum.storage.exprs import get_expr_list
from astreum.consensus.transaction.code import TransactionCode

if TYPE_CHECKING:
    from astreum.consensus.transaction.model import Transaction


def get_transaction_from_storage(
    node: Any,
    transaction_id: bytes,
    contact=None,
    block_hash=None,
) -> "Transaction":
    from astreum.consensus.transaction.model import Transaction as TxModel

    scope = {} if block_hash is None else {"block_hash": block_hash}
    header = get_expr_list(node, transaction_id, contact=contact, **scope)
    if header is None:
        raise ValueError("unable to load transaction from storage")
    if not header._tag == "link":
        raise ValueError("transaction header must be a Link")
    if header._tail is None or header._tail._tag != "symbol" or header._tail.value != "transaction":
        raise ValueError(
            f"invalid transaction type tag (got {header._tail!r})"
        )

    inner = header._head
    if inner is None or inner._tag != "link":
        raise ValueError("transaction inner header must be a Link")

    inner_nodes, missed = resolve_list_exprs(node, inner, contact=contact, **scope)
    if missed:
        raise ValueError(
            f"unable to resolve transaction header (missed={[h.hex()[:8] for h in missed]})"
        )
    if len(inner_nodes) != 2:
        raise ValueError(
            f"malformed transaction header length (got={len(inner_nodes)}, expected=2)"
        )

    body, sig = inner_nodes
    if not sig._tag == "bytes":
        raise ValueError("invalid transaction signature: expected Bytes")
    signature_bytes = sig.value
    if not body._tag == "link":
        raise ValueError("transaction body must be a Link chain")

    body_nodes, missed = resolve_list_exprs(node, body, contact=contact, **scope)
    if missed:
        raise ValueError(
            f"unable to resolve transaction body (missed={[h.hex()[:8] for h in missed]})"
        )
    if len(body_nodes) != 8:
        raise ValueError(
            f"malformed transaction body length (got={len(body_nodes)}, expected=8)"
        )

    (
        amount_node,
        chain_id_node,
        code_node,
        cost_limit_node,
        counter_node,
        data_node,
        recipient_node,
        sender_node,
    ) = body_nodes

    if not get_expr_tag(amount_node, node, contact, block_hash) == "int":
        raise ValueError("expected Int for amount")
    if not get_expr_tag(chain_id_node, node, contact, block_hash) == "int":
        raise ValueError("expected Int for chain_id")
    if not get_expr_tag(code_node, node, contact, block_hash) == "int":
        raise ValueError("expected Int for code")
    if not get_expr_tag(cost_limit_node, node, contact, block_hash) == "int":
        raise ValueError("expected Int for cost_limit")
    if not get_expr_tag(counter_node, node, contact, block_hash) == "int":
        raise ValueError("expected Int for counter")
    if not recipient_node._tag == "bytes":
        raise ValueError("expected Bytes for recipient")
    if not sender_node._tag == "bytes":
        raise ValueError("expected Bytes for sender")

    # The handlers read the data whole: a transaction fetched cell by cell has
    # children by hash, which are in the transaction's block. What cannot be
    # fetched stays a hash; the handler for its code fails the transaction.
    resolve_inner_exprs(node, data_node, contact=contact, **scope)

    tx = TxModel(
        chain_id=get_expr_value(chain_id_node, node, contact, block_hash),
        amount=get_expr_value(amount_node, node, contact, block_hash),
        code=TransactionCode(get_expr_value(code_node, node, contact, block_hash)),
        counter=get_expr_value(counter_node, node, contact, block_hash),
        cost_limit=get_expr_value(cost_limit_node, node, contact, block_hash),
        data=data_node,
        recipient=recipient_node.value,
        sender=sender_node.value,
        signature=signature_bytes,
        body_hash=body.hash(),
        expr_id=transaction_id,
        hash=transaction_id,
    )
    tx._expr = header
    return tx
