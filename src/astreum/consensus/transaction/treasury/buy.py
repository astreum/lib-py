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
from astreum.consensus.models.statistics import all_time_rate_fraction
from astreum.consensus.transaction.treasury.record import (
    load_loan_record,
    load_user_record,
)
from astreum.consensus.transaction.treasury.utils import _trie_exprs
from astreum.expression.helpers import get_expr_builtin_tag


LOAN_TRANSACTION_ID_SIZE = 32


def _buyout_price(
    *,
    payment_amount: int,
    rate_fraction: tuple[int, int],
) -> int | None:
    """Price a perpetual loan's remaining stream at the all-time rate.

    The plan's formula is ``(payment_amount * stake + fee - 1) // fee``,
    which is ``ceil(payment_amount / r(h))`` with ``r(h) = fee / stake``.

    Args:
        payment_amount: The loan's per-block payment.
        rate_fraction: ``(fee, stake)`` from the previous block's statistics.

    Returns:
        The buyout price, or ``None`` when an input cannot be priced.
    """
    fee, stake = rate_fraction
    if payment_amount <= 0 or fee <= 0 or stake <= 0:
        return None
    return (payment_amount * stake + fee - 1) // fee


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

    if loan.owner == transaction.sender:
        return STATUS_FAILED

    rate_fraction = all_time_rate_fraction(block)
    if rate_fraction is None:
        return STATUS_FAILED

    price = _buyout_price(
        payment_amount=loan.payment_amount,
        rate_fraction=rate_fraction,
    )
    if price is None or price <= 0 or transaction.amount < price:
        return STATUS_FAILED

    buyer_account = block.accounts.get_account(address=transaction.sender, node=node)
    if buyer_account is None or buyer_account.balance < price:
        return STATUS_FAILED

    # Resolve the payee before any balance moves. A missing owner account is a
    # failure, and it must leave the buyer's balance untouched.
    if loan.owner == TREASURY_ADDRESS:
        payee_account = treasury_account
    else:
        payee_account = block.accounts.get_account(address=loan.owner, node=node)
        if payee_account is None:
            return STATUS_FAILED

    # ``TREASURY_BUY`` has no core transfer (``apply`` sets its transfer to
    # zero), so the handler moves the money. The buyer pays the price; the
    # plan's refund is the ``transaction.amount - price`` excess, which never
    # leaves the buyer's account.
    buyer_account.balance -= price
    block.accounts.set_account(transaction.sender, buyer_account)

    payee_account.balance += price
    block.accounts.set_account(loan.owner, payee_account)

    updated_loan = replace(loan, owner=transaction.sender)
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
