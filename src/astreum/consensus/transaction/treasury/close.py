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

    total_payment_count = loan.payment_count
    if total_payment_count <= 0:
        return STATUS_FAILED

    current_block_number = _current_height(block)
    final_payment_block_number = (
        loan.creation_block_number
        + loan.payment_interval_blocks * loan.payment_count
    )

    if loan.loan_type == LoanType.UNSECURED:
        # Overdue installments are written off (defaulted), never
        # "caught up" at face value the way SECURED loans are below.
        missed_delta = 0
        next_payment = loan.next_payment_block_number
        while next_payment != 0 and next_payment < current_block_number:
            missed_delta += 1
            if next_payment == final_payment_block_number:
                next_payment = 0
            else:
                next_payment += loan.payment_interval_blocks

        if next_payment == 0:
            paid_after = total_payment_count
        else:
            if loan.payment_interval_blocks <= 0:
                return STATUS_FAILED
            paid_after = (
                (next_payment - loan.creation_block_number)
                // loan.payment_interval_blocks
            ) - 1

        remaining_count = total_payment_count - paid_after
        remaining_principal = (
            loan.discounted_amount * remaining_count // total_payment_count
            if remaining_count > 0
            else 0
        )
        total_cost = remaining_principal
        write_off_amount = missed_delta * loan.payment_amount

        if remaining_count <= 0:
            # Every installment was overdue and written off — nothing left
            # to charge. Refund the submitted amount (fees still apply).
            sender_account = block.accounts.get_account(address=transaction.sender, node=node)
            if sender_account is None:
                return STATUS_FAILED
            sender_account.balance += transaction.amount
            block.accounts.set_account(transaction.sender, sender_account)
            credit_treasury = False
            refund_to_sender = 0
        else:
            if total_cost <= 0 or transaction.amount < total_cost:
                return STATUS_FAILED
            credit_treasury = True
            refund_to_sender = transaction.amount - total_cost
    else:
        missed_delta = 0
        write_off_amount = 0
        catchup_amount = 0
        next_payment = loan.next_payment_block_number

        while next_payment > 0 and next_payment <= current_block_number:
            catchup_amount += loan.payment_amount
            next_payment += loan.payment_interval_blocks
            if next_payment > final_payment_block_number:
                next_payment = 0
                break

        remaining_principal = 0

        if next_payment > 0:
            if loan.payment_interval_blocks <= 0:
                return STATUS_FAILED
            paid_after = (
                (next_payment - loan.creation_block_number)
                // loan.payment_interval_blocks
            ) - 1
            remaining_count = total_payment_count - paid_after
            if remaining_count <= 0:
                return STATUS_FAILED
            remaining_principal = loan.discounted_amount * remaining_count // total_payment_count

        total_cost = catchup_amount + remaining_principal
        if total_cost <= 0 or transaction.amount < total_cost:
            return STATUS_FAILED
        credit_treasury = True
        refund_to_sender = 0

    updated_loan = replace(
        loan,
        next_payment_block_number=0,
        missed_count=loan.missed_count + missed_delta,
    )
    updated_loan_head = updated_loan.expr().hash()
    put_in_radix_tree(loans_trie, node, loan_transaction_id, updated_loan_head)
    loan_exprs, _ = resolve_inner_exprs(node, updated_loan.expr())

    if loan.loan_type == LoanType.UNSECURED:
        updated_user_record = replace(
            user_record,
            loans_root_hash=loans_trie.root_hash or ZERO32,
            defaulted=user_record.defaulted + write_off_amount,
        )
    else:
        stake_excess = transaction.amount - total_cost
        updated_user_record = replace(
            user_record,
            balance=user_record.balance + stake_excess,
            loans_root_hash=loans_trie.root_hash or ZERO32,
        )
    updated_user_record_head = updated_user_record.expr().hash()
    put_in_radix_tree(treasury_account.data, node, transaction.sender, updated_user_record_head)
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32

    if loan.loan_type == LoanType.UNSECURED:
        if credit_treasury:
            if loan.owner == TREASURY_ADDRESS:
                treasury_account.balance += total_cost
            else:
                owner_account = block.accounts.get_account(address=loan.owner, node=node)
                if owner_account is None:
                    return STATUS_FAILED
                owner_account.balance += total_cost
                block.accounts.set_account(loan.owner, owner_account)
    else:
        if credit_treasury:
            if loan.owner == TREASURY_ADDRESS:
                treasury_account.balance += transaction.amount
            else:
                owner_account = block.accounts.get_account(address=loan.owner, node=node)
                if owner_account is None:
                    return STATUS_FAILED
                owner_account.balance += transaction.amount
                block.accounts.set_account(loan.owner, owner_account)

    if loan.loan_type == LoanType.UNSECURED and refund_to_sender:
        sender_account = block.accounts.get_account(address=transaction.sender, node=node)
        if sender_account is not None:
            sender_account.balance += refund_to_sender
            block.accounts.set_account(transaction.sender, sender_account)

    user_record_exprs, _ = resolve_inner_exprs(node, updated_user_record.expr())
    pending_exprs = loan_exprs + _trie_exprs(loans_trie) + user_record_exprs

    if write_off_amount:
        block.global_defaulted = getattr(block, "global_defaulted", 0) + write_off_amount

    pending_exprs = pending_exprs + _return_claimed_offer_limits(
        node, treasury_account, updated_loan
    )

    block.pending_exprs.extend(pending_exprs)
    block.accounts.set_account(TREASURY_ADDRESS, treasury_account)
    return STATUS_SUCCESS
