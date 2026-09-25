from __future__ import annotations

from dataclasses import replace
from typing import Any

from astreum.expression import resolve_inner_exprs
from astreum.expression import ZERO32
from astreum.storage.radix import RadixTree, get_from_radix_tree, put_in_radix_tree
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.from_storage import get_transaction_from_storage
from astreum.consensus.transaction.treasury.record import (
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
)
from astreum.consensus.transaction.treasury.utils import (
    _paid_payment_count,
    _trie_exprs,
)


LOAN_TRANSACTION_ID_SIZE = 32


def _current_height(block: object) -> int:
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

    if transaction.data._tag != "link" or transaction.data._head is None:
        return STATUS_FAILED
    data_head = transaction.data._head
    if data_head._tag != "link" or data_head._head_hash is None:
        return STATUS_FAILED
    loan_transaction_id = data_head._head_hash
    if len(loan_transaction_id) != LOAN_TRANSACTION_ID_SIZE:
        return STATUS_FAILED

    borrower_tx = get_transaction_from_storage(node, loan_transaction_id)
    if borrower_tx is None:
        return STATUS_FAILED
    borrower = borrower_tx.sender

    treasury_account = block.accounts.get_account(address=TREASURY_ADDRESS, node=node)
    if treasury_account is None:
        return STATUS_FAILED

    borrower_record_head = get_from_radix_tree(treasury_account.data, node, borrower)
    borrower_record = TreasuryUserRecord.from_storage(node, borrower_record_head or ZERO32)
    if borrower_record is None or borrower_record.loans_root_hash == ZERO32:
        return STATUS_FAILED

    loans_trie = RadixTree(root_hash=borrower_record.loans_root_hash)
    loan_record_head = get_from_radix_tree(loans_trie, node, loan_transaction_id)
    loan = TreasuryLoanRecord.from_storage(node, loan_record_head or ZERO32)
    if loan is None or loan.next_payment_block_number == 0:
        return STATUS_FAILED

    if loan.owner == TREASURY_ADDRESS:
        return STATUS_FAILED
    if loan.owner != transaction.sender:
        return STATUS_FAILED

    current_height = _current_height(block)

    final_payment_block_number = (
        loan.creation_block_number
        + loan.payment_interval_blocks * loan.payment_count
    )

    if current_height < loan.next_payment_block_number:
        missed_count = 0
    else:
        missed_count = (
            (current_height - loan.next_payment_block_number) // loan.payment_interval_blocks + 1
        )

    total_payment_count = loan.payment_count
    paid_so_far = _paid_payment_count(loan)
    if paid_so_far is None or total_payment_count is None:
        return STATUS_FAILED
    remaining_count = total_payment_count - paid_so_far
    if remaining_count < 0:
        return STATUS_FAILED

    if missed_count > remaining_count:
        missed_count = remaining_count

    if missed_count == 0:
        return STATUS_FAILED

    payout = missed_count * loan.payment_amount

    if treasury_account.balance < payout:
        return STATUS_FAILED
    treasury_account.balance -= payout

    owner_account = block.accounts.get_account(address=loan.owner, node=node)
    if owner_account is None:
        return STATUS_FAILED
    owner_account.balance += payout
    block.accounts.set_account(loan.owner, owner_account)

    next_payment_block_number = loan.next_payment_block_number
    for _ in range(missed_count):
        if next_payment_block_number == final_payment_block_number:
            next_payment_block_number = 0
            break
        next_payment_block_number += loan.payment_interval_blocks
        if next_payment_block_number > final_payment_block_number:
            return STATUS_FAILED

    updated_loan = replace(
        loan,
        next_payment_block_number=next_payment_block_number,
    )
    updated_loan_head = updated_loan.expr().hash()
    put_in_radix_tree(loans_trie, node, loan_transaction_id, updated_loan_head)
    loan_exprs, _ = resolve_inner_exprs(node, updated_loan.expr())

    updated_borrower_record = replace(
        borrower_record,
        loans_root_hash=loans_trie.root_hash or ZERO32,
    )

    if loan.loan_type == LoanType.UNSECURED:
        updated_borrower_record = replace(
            updated_borrower_record,
            defaulted=borrower_record.defaulted + payout,
        )
        block.global_defaulted = getattr(block, "global_defaulted", 0) + payout

    updated_borrower_record_head = updated_borrower_record.expr().hash()
    put_in_radix_tree(treasury_account.data, node, borrower, updated_borrower_record_head)
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    borrower_record_exprs, _ = resolve_inner_exprs(node, updated_borrower_record.expr())

    pending_exprs = loan_exprs + _trie_exprs(loans_trie) + borrower_record_exprs
    block.pending_exprs.extend(pending_exprs)
    block.accounts.set_account(TREASURY_ADDRESS, treasury_account)

    return STATUS_SUCCESS
