from __future__ import annotations

from dataclasses import replace
from typing import Any

from astreum.expression import resolve_inner_exprs
from astreum.expression import ZERO32
from astreum.storage.radix import RadixTree, put_in_radix_tree
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.from_storage import get_transaction_from_storage
from astreum.consensus.transaction.treasury.record import (
    load_loan_record,
    load_user_record,
    LoanType,
)
from astreum.consensus.transaction.treasury.utils import (
    _overdue_blocks,
    _trie_exprs,
)
from astreum.expression.helpers import get_expr_builtin_tag


LOAN_TRANSACTION_ID_SIZE = 32


def _current_height(block: object) -> int:
    """Get the current block height.

    Args:
        block: The block.

    Returns:
        The block height.
    """
    return int(
        getattr(
            block,
            "height",
            int(getattr(block.previous_block, "height", -1)) + 1,
        )
    )


def handle_treasury_claim(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
    transaction_hash: bytes,
) -> int:
    if (
        transaction.recipient != TREASURY_ADDRESS
        or transaction.sender == TREASURY_ADDRESS
        or transaction.amount != 0
    ):
        return STATUS_FAILED

    if get_expr_builtin_tag(transaction.data) != "link" or transaction.data.head is None:
        return STATUS_FAILED
    data_head = transaction.data.head
    if get_expr_builtin_tag(data_head) != "bytes":
        return STATUS_FAILED
    loan_transaction_id = data_head.value
    if len(loan_transaction_id) != LOAN_TRANSACTION_ID_SIZE:
        return STATUS_FAILED

    borrower_tx = get_transaction_from_storage(node, loan_transaction_id, contact=block.contact)
    if borrower_tx is None:
        return STATUS_FAILED
    borrower = borrower_tx.sender

    treasury_account = block.accounts.get_account(address=TREASURY_ADDRESS, node=node)
    if treasury_account is None:
        return STATUS_FAILED

    borrower_record = load_user_record(node, treasury_account.data, borrower, contact=block.contact)
    if borrower_record is None or borrower_record.loans_root_hash == ZERO32:
        return STATUS_FAILED

    loans_trie = RadixTree(
        root_hash=borrower_record.loans_root_hash,
        contact=block.contact,
        root_block=borrower_record.loans_root_block,
    )
    loan = load_loan_record(node, loans_trie, loan_transaction_id, contact=block.contact)
    if loan is None or loan.paid_until_block == 0:
        return STATUS_FAILED

    # Only the owner collects, and only from a loan the treasury does not own.
    if loan.owner == TREASURY_ADDRESS:
        return STATUS_FAILED
    if loan.owner != transaction.sender:
        return STATUS_FAILED

    # The plan limits a claim to a secured loan. Secured arrears are interest,
    # not a default, so a claim writes nothing off.
    if loan.loan_type != LoanType.SECURED or loan.payment_amount <= 0:
        return STATUS_FAILED

    current_height = _current_height(block)
    overdue_blocks = _overdue_blocks(loan=loan, current_height=current_height)
    payout = overdue_blocks * loan.payment_amount
    if payout <= 0:
        return STATUS_FAILED

    owner_account = block.accounts.get_account(address=loan.owner, node=node)
    if owner_account is None:
        return STATUS_FAILED

    if treasury_account.balance < payout:
        return STATUS_FAILED
    treasury_account.balance -= payout
    owner_account.balance += payout
    block.accounts.set_account(loan.owner, owner_account)

    updated_loan = replace(
        loan,
        paid_until_block=current_height,
    )
    updated_loan_head = updated_loan.expr().hash()
    put_in_radix_tree(loans_trie, node, loan_transaction_id, updated_loan_head)
    loan_exprs, _ = resolve_inner_exprs(node, updated_loan.expr(), contact=block.contact)

    updated_borrower_record = replace(
        borrower_record,
        loans_root_hash=loans_trie.root_hash or ZERO32,
        loans_root_block=loans_trie.root_block,
    )
    updated_borrower_record_head = updated_borrower_record.expr().hash()
    put_in_radix_tree(treasury_account.data, node, borrower, updated_borrower_record_head)
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    borrower_record_exprs, _ = resolve_inner_exprs(node, updated_borrower_record.expr(), contact=block.contact)

    pending_exprs = loan_exprs + _trie_exprs(loans_trie) + borrower_record_exprs
    block.pending_exprs.extend(pending_exprs)
    block.accounts.set_account(TREASURY_ADDRESS, treasury_account)

    return STATUS_SUCCESS
