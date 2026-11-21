from __future__ import annotations

from dataclasses import replace
from typing import Any

from astreum.expression import resolve_inner_exprs
from astreum.expression import ZERO32
from astreum.storage.radix import RadixTree, get_from_radix_tree, put_in_radix_tree
from astreum.consensus.block.rate import calculate_quote_rate
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.models.statistics import BlockStatistics
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


def _claim_cost_buyout_price(
    loan: TreasuryLoanRecord, block: object
) -> int | None:
    """Return a claim-cost loan's buyout price ``ceil(principal / r_q)``.

    A claim-cost loan's principal is the storage fee the chain financed
    for the claim transaction. The buyout pays that principal divided by
    the Plan A quote ``r_q``, so the borrower owes
    ``ceil(principal / r_q)``. The division is exact rational arithmetic;
    the ceiling is an integer division on the scaled principal.

    Args:
        loan: The open claim-cost loan.
        block: The block whose height and previous statistics give the
            quote.

    Returns:
        The buyout price, or `None` when the quote or the principal cannot
        price the loan. A loan is never bought out for nothing.
    """
    quote = calculate_quote_rate(block)
    if quote <= 0 or loan.discounted_amount <= 0:
        return None
    return (
        loan.discounted_amount * quote.denominator + quote.numerator - 1
    ) // quote.numerator


def _refund_excess(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
    total_cost: int,
) -> bool:
    """Return the amount above *total_cost* to the sender.

    A close transfers the whole transaction amount to the treasury at
    settlement, so the handler returns the excess before it pays the loan.
    Every close path shares this refund.

    Args:
        node: Storage node used by the account cache.
        block: The block whose accounts receive the refund.
        transaction: The close transaction; the refund goes to its sender.
        total_cost: The price of the close.

    Returns:
        True when the refund is done or not needed, False when the sender
        account is missing. A False return fails the handler.
    """
    refund_to_sender = transaction.amount - total_cost
    if not refund_to_sender:
        return True
    sender_account = block.accounts.get_account(
        address=transaction.sender, node=node
    )
    if sender_account is None:
        return False
    sender_account.balance += refund_to_sender
    block.accounts.set_account(transaction.sender, sender_account)
    return True


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

    if loan.loan_type == LoanType.CLAIM_COST:
        # A claim-cost loan buys out at the quote: ceil(principal / r_q),
        # paid to the loan's owner. The price is the loan's whole value, so
        # the treasury keeps no principal of its own, and the close writes
        # no arrears off: nothing of the loan remains owed.
        total_cost = _claim_cost_buyout_price(loan, block)
        if (
            total_cost is None
            or total_cost <= 0
            or transaction.amount < total_cost
        ):
            return STATUS_FAILED
        write_off_amount = 0
        missed_delta = 0
        interest_delta = 0

        # Resolve the payee before any balance moves.
        payee_account = treasury_account
        if loan.owner != TREASURY_ADDRESS:
            payee_account = block.accounts.get_account(address=loan.owner, node=node)
            if payee_account is None:
                return STATUS_FAILED

        if not _refund_excess(
            node=node,
            block=block,
            transaction=transaction,
            total_cost=total_cost,
        ):
            return STATUS_FAILED

        payee_account.balance += total_cost
        if loan.owner != TREASURY_ADDRESS:
            block.accounts.set_account(loan.owner, payee_account)
    else:
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

        if not _refund_excess(
            node=node,
            block=block,
            transaction=transaction,
            total_cost=total_cost,
        ):
            return STATUS_FAILED

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
        block.statistics = (
            block.statistics or BlockStatistics()
        ).with_write_off(write_off_amount)

    block.pending_exprs.extend(pending_exprs)
    block.accounts.set_account(TREASURY_ADDRESS, treasury_account)
    return STATUS_SUCCESS
