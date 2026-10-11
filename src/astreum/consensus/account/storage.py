from __future__ import annotations

from typing import Any

from astreum.expression import Expr, get_referenced_hash, resolve_list_exprs
from astreum.storage.exprs import get_expr_list
from astreum.consensus.account.create import create_account
from astreum.consensus.account.model import Account
from astreum.expression.helpers import get_expr_builtin_tag


def get_account_from_storage(node: Any, expr_id: bytes, contact=None) -> Account:
    """Load an account from storage.

    Args:
        node: The node to load from.
        expr_id: The expression identifier bytes.
        contact: Optional contact for fetching.

    Returns:
        The loaded Account instance.

    Raises:
        ValueError: If unable to load or malformed account.
    """
    header = get_expr_list(node, expr_id, contact=contact)
    if header is None:
        raise ValueError("unable to load account from storage")
    if not get_expr_builtin_tag(header) == "link":
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

    if not get_expr_builtin_tag(data_node) == "link":
        raise ValueError("expected Link for data_hash")
    if not get_expr_builtin_tag(counter_node) == "int":
        raise ValueError("expected Int for counter")
    if not get_expr_builtin_tag(code_node) == "link":
        raise ValueError("expected Link for code_hash")
    if not get_expr_builtin_tag(channels_node) == "link":
        raise ValueError("expected Link for channels_hash")
    if not get_expr_builtin_tag(balance_node) == "int":
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
