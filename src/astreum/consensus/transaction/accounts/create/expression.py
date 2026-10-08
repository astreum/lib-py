from __future__ import annotations

from typing import Any

from astreum.consensus.account import create_account


def handle_expression_account_create(
    node: Any,
    block: Any,
    transaction: Any,
    transaction_hash: bytes,
) -> bool:
    """Create an expression account addressed by its create transaction hash."""
    expression_address = transaction_hash

    if transaction.data._tag != "link" or transaction.data._head is None:
        return False
    data_head = transaction.data._head
    if data_head._tag != "link":
        return False
    # The program is named by hash, or already loaded (a transaction read from
    # a message or fetched whole): its hash is the same.
    if data_head._head_hash is not None:
        program_hash = data_head._head_hash
    elif data_head._head is not None:
        program_hash = data_head._head.hash()
    else:
        return False

    if transaction.recipient != b"":
        return False
    if len(expression_address) != 32 or len(program_hash) != 32:
        return False
    if block.accounts.get_account(address=expression_address, node=node) is not None:
        return False

    expression_account = create_account(
        balance=int(transaction.amount),
        code_hash=program_hash,
    )
    block.accounts.set_account(expression_address, expression_account)
    return True
