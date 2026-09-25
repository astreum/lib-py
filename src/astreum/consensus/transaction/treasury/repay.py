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
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
)
from astreum.consensus.transaction.treasury.utils import (
    _interest_paid_delta,
    _paid_payment_count,
    _return_claimed_offer_limits,
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


def handle_treasury_repay(
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

    if transaction.data._tag != "link" or transaction.data._head is None:
        return STATUS_FAILED
    data_head = transaction.data._head
    if data_head._tag != "link" or data_head._head_hash is None:
        return STATUS_FAILED
    loan_transaction_id = data_head._head_hash
    if len(loan_transaction_id) != LOAN_TRANSACTION_ID_SIZE:
        return STATUS_FAILED

    user_record_head = get_from_radix_tree(treasury_account.data, node, transaction.sender)
    user_record = TreasuryUserRecord.from_storage(node, user_record_head or ZERO32)
    if user_record is None or user_record.loans_root_hash == ZERO32:
        return STATUS_FAILED

    loans_trie = RadixTree(root_hash=user_record.loans_root_hash)
    loan_record_head = get_from_radix_tree(loans_trie, node, loan_transaction_id)
    loan = TreasuryLoanRecord.from_storage(node, loan_record_head or ZERO32)
    if loan is None or loan.next_payment_block_number == 0:
        return STATUS_FAILED
    if loan.payment_amount <= 0 or transaction.amount % loan.payment_amount != 0:
        return STATUS_FAILED

    total_payment_count = loan.payment_count
    paid_before = _paid_payment_count(loan)
    if total_payment_count <= 0 or paid_before is None:
        return STATUS_FAILED
    if paid_before < 0 or paid_before >= total_payment_count:
        return STATUS_FAILED

    final_payment_block_number = (
        loan.creation_block_number
        + loan.payment_interval_blocks * loan.payment_count
    )

    # --- unsecured-only: write off any installment already past its due
    # block before applying this transaction's own payment. ---
    missed_delta = 0
    written_off_pointer = loan.next_payment_block_number
    if loan.loan_type == LoanType.UNSECURED:
        current_height = _current_height(block)
        while written_off_pointer != 0 and written_off_pointer < current_height:
            missed_delta += 1
            if written_off_pointer == final_payment_block_number:
                written_off_pointer = 0
            else:
                written_off_pointer += loan.payment_interval_blocks

    if written_off_pointer == 0:
        paid_after_writeoff = total_payment_count
    else:
        paid_after_writeoff = (
            (written_off_pointer - loan.creation_block_number)
            // loan.payment_interval_blocks
        ) - 1

    if written_off_pointer == 0:
        # Every remaining installment was overdue: nothing left to charge.
        # Refund the submitted amount (fees are still deducted normally by
        # the caller) rather than failing the transaction.
        next_payment_block_number = 0
        paid_after = paid_after_writeoff
        sender_account = block.accounts.get_account(address=transaction.sender, node=node)
        if sender_account is None:
            return STATUS_FAILED
        sender_account.balance += transaction.amount
        block.accounts.set_account(transaction.sender, sender_account)
        credit_treasury = False
    else:
        payment_count = transaction.amount // loan.payment_amount
        next_payment_block_number = written_off_pointer
        remaining_payments = payment_count
        while remaining_payments > 0:
            if next_payment_block_number == 0:
                return STATUS_FAILED
            if next_payment_block_number == final_payment_block_number:
                next_payment_block_number = 0
                remaining_payments -= 1
                break
            next_payment_block_number += loan.payment_interval_blocks
            if next_payment_block_number > final_payment_block_number:
                return STATUS_FAILED
            remaining_payments -= 1

        if remaining_payments > 0:
            return STATUS_FAILED

        if next_payment_block_number == 0:
            paid_after = total_payment_count
        else:
            paid_after = (
                (next_payment_block_number - loan.creation_block_number)
                // loan.payment_interval_blocks
            ) - 1
        if paid_after > total_payment_count:
            return STATUS_FAILED
        credit_treasury = True

    interest_delta = _interest_paid_delta(
        loan=loan,
        paid_before=paid_after_writeoff,
        paid_after=paid_after,
        total_payment_count=total_payment_count,
    )
    if interest_delta is None:
        return STATUS_FAILED

    updated_loan = replace(
        loan,
        next_payment_block_number=next_payment_block_number,
        missed_count=loan.missed_count + missed_delta,
    )
    updated_loan_head = updated_loan.expr().hash()
    put_in_radix_tree(loans_trie, node, loan_transaction_id, updated_loan_head)
    loan_exprs, _ = resolve_inner_exprs(node, updated_loan.expr())

    write_off_amount = missed_delta * loan.payment_amount
    updated_user_record = replace(
        user_record,
        loans_root_hash=loans_trie.root_hash or ZERO32,
        total_interest_paid=user_record.total_interest_paid + interest_delta,
        defaulted=user_record.defaulted + write_off_amount,
    )
    updated_user_record_head = updated_user_record.expr().hash()
    put_in_radix_tree(treasury_account.data, node, transaction.sender, updated_user_record_head)
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    if credit_treasury:
        if loan.owner == TREASURY_ADDRESS:
            treasury_account.balance += transaction.amount
        else:
            owner_account = block.accounts.get_account(address=loan.owner, node=node)
            if owner_account is None:
                return STATUS_FAILED
            owner_account.balance += transaction.amount
            block.accounts.set_account(loan.owner, owner_account)
    user_record_exprs, _ = resolve_inner_exprs(node, updated_user_record.expr())

    pending_exprs = loan_exprs + _trie_exprs(loans_trie) + user_record_exprs

    if write_off_amount:
        block.global_defaulted = getattr(block, "global_defaulted", 0) + write_off_amount

    if next_payment_block_number == 0:
        pending_exprs = pending_exprs + _return_claimed_offer_limits(
            node, treasury_account, updated_loan
        )

    block.pending_exprs.extend(pending_exprs)
    block.accounts.set_account(TREASURY_ADDRESS, treasury_account)
    return STATUS_SUCCESS
