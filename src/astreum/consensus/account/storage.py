from __future__ import annotations

from typing import Any

from astreum.expression import Expr, get_referenced_hash, resolve_list_exprs
from astreum.storage.exprs import get_expr_list
from astreum.consensus.account.create import create_account
from astreum.consensus.account.model import Account


def get_account_from_storage(node: Any, expr_id: bytes, contact=None) -> Account:

    Args:
        node (Any): Description.
        expr_id (bytes): Description.
        contact: Description.

    header = get_expr_list(node, expr_id, contact=contact)
    if header is None:
        raise ValueError("unable to load account from storage")
    if not header._tag == "link":
        raise ValueError("account header must be a Link")

    header_nodes, missed = resolve_list_exprs(node, header, contact=contact)
    if missed:
        raise ValueError(
            f"unable to resolve account (missed={[h.hex()[:8] for h in missed]})"
        )
    if len(header_nodes) != 5:
        raise ValueError(
            f"malformed account length (got={len(header_nodes)}, expected=5)"
        )

    data_node, counter_node, code_node, channels_node, balance_node = header_nodes

    if not data_node._tag == "link":
        raise ValueError("expected Link for data_hash")
    if not counter_node._tag == "int":
        raise ValueError("expected Int for counter")
    if not code_node._tag == "link":
        raise ValueError("expected Link for code_hash")
    if not channels_node._tag == "link":
        raise ValueError("expected Link for channels_hash")
    if not balance_node._tag == "int":
        raise ValueError("expected Int for balance")

    account = create_account(
        balance=balance_node.value,
        data_hash=get_referenced_hash(data_node),
        channels_hash=get_referenced_hash(channels_node),
        counter=counter_node.value,
        code_hash=get_referenced_hash(code_node),
        contact=contact,
    )
    account._expr = header
    return account
