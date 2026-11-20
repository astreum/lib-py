from __future__ import annotations

from dataclasses import replace
from typing import Any

from astreum.expression import resolve_inner_exprs
from astreum.expression import ZERO32
from astreum.storage.radix import RadixTree, get_from_radix_tree, put_in_radix_tree
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.treasury.record import (
    load_loan_record,
    load_user_record,
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
)
from astreum.consensus.transaction.treasury.utils import (
    _overdue_blocks,
    _release_guarantees,
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


def handle_treasury_close(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
) -> int:
    if (
        transaction.recipient != TREASURY_ADDRESS
        or transaction.sender == TREASURY_ADDRESS
        or transaction.amount <= 0
    ):
        return STATUS_FAILED

    treasury_account = block.accounts.get_account(address=TREASURY_ADDRESS, node=node)
    if treasury_account is None:
        return STATUS_FAILED

    if get_expr_builtin_tag(transaction.data) != "link" or transaction.data.head is None:
        return STATUS_FAILED
    data_head = transaction.data.head
    if get_expr_builtin_tag(data_head) != "bytes":
        return STATUS_FAILED
    loan_transaction_id = data_head.value
    if len(loan_transaction_id) != LOAN_TRANSACTION_ID_SIZE:
        return STATUS_FAILED

    user_record = load_user_record(node, treasury_account.data, transaction.sender, contact=block.contact)
    if user_record is None or user_record.loans_root_hash == ZERO32:
        return STATUS_FAILED

    loans_trie = RadixTree(
        root_hash=user_record.loans_root_hash,
        contact=block.contact,
        root_block=user_record.loans_root_block,
    )
    loan = load_loan_record(node, loans_trie, loan_transaction_id, contact=block.contact)
    if loan is None or loan.paid_until_block == 0:
        return STATUS_FAILED
    if loan.payment_amount <= 0:
        return STATUS_FAILED
    if loan.paid_until_block < loan.creation_block_number:
        return STATUS_FAILED

    current_height = _current_height(block)
    overdue_blocks = _overdue_blocks(loan=loan, current_height=current_height)

    if loan.loan_type == LoanType.SECURED:
        # A secured loan pays its arrears at face value; they are interest.
        catchup_amount = overdue_blocks * loan.payment_amount
        write_off_amount = 0
        missed_delta = 0
        interest_delta = catchup_amount
        total_cost = loan.discounted_amount + catchup_amount
    else:
        # An unsecured loan's arrears are written off, never collected.
        catchup_amount = 0
        write_off_amount = overdue_blocks * loan.payment_amount
        missed_delta = overdue_blocks
        interest_delta = 0
        total_cost = loan.discounted_amount

    if transaction.amount < total_cost:
        return STATUS_FAILED
    refund_to_sender = transaction.amount - total_cost

    if refund_to_sender:
        sender_account = block.accounts.get_account(address=transaction.sender, node=node)
        if sender_account is None:
            return STATUS_FAILED
        sender_account.balance += refund_to_sender
        block.accounts.set_account(transaction.sender, sender_account)

    if loan.owner == TREASURY_ADDRESS:
        # The treasury keeps the principal and, being the owner, the catch-up.
        treasury_account.balance += total_cost
    elif loan.loan_type == LoanType.SECURED:
        # The principal always returns to the treasury; the loan owner
        # receives only the catch-up interest.
        treasury_account.balance += loan.discounted_amount
        if catchup_amount:
            owner_account = block.accounts.get_account(address=loan.owner, node=node)
            if owner_account is None:
                return STATUS_FAILED
            owner_account.balance += catchup_amount
            block.accounts.set_account(loan.owner, owner_account)
    else:
        owner_account = block.accounts.get_account(address=loan.owner, node=node)
        if owner_account is None:
            return STATUS_FAILED
        owner_account.balance += total_cost
        block.accounts.set_account(loan.owner, owner_account)

    updated_loan = replace(
        loan,
        paid_until_block=0,
        missed_count=loan.missed_count + missed_delta,
    )
    updated_loan_head = updated_loan.expr().hash()
    put_in_radix_tree(loans_trie, node, loan_transaction_id, updated_loan_head)
    loan_exprs, _ = resolve_inner_exprs(node, updated_loan.expr(), contact=block.contact)

    updated_user_record = replace(
        user_record,
        loans_root_hash=loans_trie.root_hash or ZERO32,
        loans_root_block=loans_trie.root_block,
        total_interest_paid=user_record.total_interest_paid + interest_delta,
        defaulted=user_record.defaulted + write_off_amount,
    )
    updated_user_record_head = updated_user_record.expr().hash()
    put_in_radix_tree(treasury_account.data, node, transaction.sender, updated_user_record_head)
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32

    user_record_exprs, _ = resolve_inner_exprs(node, updated_user_record.expr(), contact=block.contact)
    pending_exprs = loan_exprs + _trie_exprs(loans_trie) + user_record_exprs

    if write_off_amount:
        block.global_defaulted = getattr(block, "global_defaulted", 0) + write_off_amount

    pending_exprs = pending_exprs + _release_guarantees(
        node, treasury_account, updated_loan
    )

    block.pending_exprs.extend(pending_exprs)
    block.accounts.set_account(TREASURY_ADDRESS, treasury_account)
    return STATUS_SUCCESS
