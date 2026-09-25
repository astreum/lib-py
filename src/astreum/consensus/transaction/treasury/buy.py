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
from astreum.consensus.block.rate_window import windowed_rate_fraction
from astreum.consensus.transaction.treasury.discount import calculate_discounted_amount
from astreum.consensus.transaction.treasury.record import (
    TreasuryLoanRecord,
    TreasuryUserRecord,
)
from astreum.consensus.transaction.treasury.utils import (
    _remaining_payment_count,
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


def handle_treasury_buy(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
    transaction_hash: bytes,
) -> int:
    if (
        transaction.recipient != TREASURY_ADDRESS
        or transaction.sender == TREASURY_ADDRESS
        or transaction.amount <= 0
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

    if loan.owner == transaction.sender:
        return STATUS_FAILED

    remaining_count = _remaining_payment_count(loan)
    if remaining_count is None or remaining_count <= 0:
        return STATUS_FAILED

    remaining_duration = loan.payment_interval_blocks * remaining_count
    rate_window = 1
    power = 1
    while power * 2 <= remaining_duration:
        power *= 2
    rate_window = power

    rate_fraction = windowed_rate_fraction(block, rate_window)
    if rate_fraction is None:
        return STATUS_FAILED

    price = calculate_discounted_amount(
        payment_amount=loan.payment_amount,
        payment_interval_blocks=loan.payment_interval_blocks,
        payment_count=remaining_count,
        rate_numerator=rate_fraction[0],
        rate_denominator=rate_fraction[1],
    )
    if price is None or price <= 0 or transaction.amount < price:
        return STATUS_FAILED

    refund = transaction.amount - price

    if loan.owner == TREASURY_ADDRESS:
        if treasury_account.balance < price:
            return STATUS_FAILED
        treasury_account.balance -= price
    else:
        current_owner_account = block.accounts.get_account(address=loan.owner, node=node)
        if current_owner_account is None:
            return STATUS_FAILED
        if current_owner_account.balance < price:
            return STATUS_FAILED
        current_owner_account.balance -= price
        block.accounts.set_account(loan.owner, current_owner_account)

    if refund > 0:
        buyer_account = block.accounts.get_account(address=transaction.sender, node=node)
        if buyer_account is None:
            return STATUS_FAILED
        buyer_account.balance += refund
        block.accounts.set_account(transaction.sender, buyer_account)

    updated_loan = replace(loan, owner=transaction.sender)
    updated_loan_head = updated_loan.expr().hash()
    put_in_radix_tree(loans_trie, node, loan_transaction_id, updated_loan_head)
    loan_exprs, _ = resolve_inner_exprs(node, updated_loan.expr())

    updated_borrower_record = replace(
        borrower_record,
        loans_root_hash=loans_trie.root_hash or ZERO32,
    )
    updated_borrower_record_head = updated_borrower_record.expr().hash()
    put_in_radix_tree(treasury_account.data, node, borrower, updated_borrower_record_head)
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    borrower_record_exprs, _ = resolve_inner_exprs(node, updated_borrower_record.expr())

    pending_exprs = loan_exprs + _trie_exprs(loans_trie) + borrower_record_exprs
    block.pending_exprs.extend(pending_exprs)
    block.accounts.set_account(TREASURY_ADDRESS, treasury_account)

    return STATUS_SUCCESS
