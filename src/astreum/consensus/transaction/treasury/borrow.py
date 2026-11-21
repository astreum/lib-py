from __future__ import annotations

from dataclasses import replace
from typing import Any

from astreum.expression import Expr, resolve_inner_exprs, get_expr_builtin_tag
from astreum.expression import ZERO32
from astreum.storage.radix import (
    RadixTree,
    get_from_radix_tree,
    get_all_from_radix_tree_with_blocks,
    put_in_radix_tree,
)
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.models.statistics import (
    BlockStatistics,
    all_time_rate_fraction,
)
from astreum.consensus.transaction.treasury.record import (
    load_user_record,
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
    TreasuryBorrowRequest,
)
from astreum.consensus.transaction.treasury.utils import (
    _data_nodes,
    _trie_exprs,
    _current_height,
    insurance_interval_fee as _insurance_interval_fee,
)


def _extend_pending_exprs(block: object, exprs: list[Expr]) -> None:
    """Extend block's pending expressions.

    Args:
        block: The block.
        exprs: The expressions to add.

    Returns: None
    """
    block.pending_exprs.extend(exprs)


def secured_loan_remaining_total(
    *,
    node: Any,
    loans_root_hash: bytes,
    contact=None,
    loans_root_block: bytes | None = None,
) -> int | None:
    """Sum the open principal of a borrower's secured loans.

    The perpetual record has no remaining payment count. The collateral a
    secured loan consumes is the principal ``discounted_amount`` the treasury
    paid out, and only while the loan is open (``paid_until_block > 0``).

    Args:
        node: Storage node used to resolve trie and record exprs.
        loans_root_hash: Root hash of the borrower's loans trie.
        contact: Optional contact for storage fetches.
        loans_root_block: Block that holds the trie root's cell, when older
            than the current block.

    Returns:
        The sum of ``discounted_amount`` over open secured loans, ``0`` for an
        empty trie, or ``None`` if a record cannot be decoded.
    """
    if not loans_root_hash or loans_root_hash == ZERO32:
        return 0

    loans_trie = RadixTree(root_hash=loans_root_hash, contact=contact, root_block=loans_root_block)
    remaining_total = 0
    for loan_record_head, loan_block in get_all_from_radix_tree_with_blocks(loans_trie, node).values():
        loan = TreasuryLoanRecord.from_storage(
            node, loan_record_head, contact=contact, block_hash=loan_block
        )
        if loan is None:
            return None
        if loan.loan_type != LoanType.SECURED or loan.paid_until_block == 0:
            continue
        remaining_total += loan.discounted_amount
    return remaining_total


def _per_block_payment(*, principal: int, rate_fraction: tuple[int, int]) -> int | None:
    """Lock the per-block payment at the all-time rate.

    The plan's formula is ``(L * fee + stake - 1) // stake``, which is
    ``ceil(L * r_bar)`` with ``r_bar = fee / stake``.

    Args:
        principal: The principal ``L``.
        rate_fraction: ``(fee, stake)`` from the previous block's statistics.

    Returns:
        The per-block payment, or ``None`` when the principal or either rate
        input is not positive.
    """
    fee, stake = rate_fraction
    if principal <= 0 or fee <= 0 or stake <= 0:
        return None
    return (principal * fee + stake - 1) // stake


def _handle_secured_borrow(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
    transaction_hash: bytes,
    sender_account: Any,
    treasury_account: Any,
    request: TreasuryBorrowRequest,
) -> int:
    rate_fraction = all_time_rate_fraction(block)
    if rate_fraction is None:
        return STATUS_FAILED

    principal = transaction.amount
    payment_amount = _per_block_payment(principal=principal, rate_fraction=rate_fraction)
    if payment_amount is None:
        return STATUS_FAILED

    user_record = load_user_record(node, treasury_account.data, transaction.sender, contact=block.contact)
    existing_secured_total = (
        None
        if user_record is None
        else secured_loan_remaining_total(
            node=node,
            loans_root_hash=user_record.loans_root_hash or ZERO32,
            contact=block.contact,
            loans_root_block=user_record.loans_root_block,
        )
    )
    if (
        user_record is None
        or existing_secured_total is None
        or user_record.balance - existing_secured_total < principal
        or treasury_account.balance < principal
    ):
        return STATUS_FAILED

    creation_block_number = _current_height(block)
    loan_record = TreasuryLoanRecord(
        creation_block_number=creation_block_number,
        loan_type=request.loan_type,
        discounted_amount=principal,
        payment_amount=payment_amount,
        paid_until_block=creation_block_number,
        owner=TREASURY_ADDRESS,
    )
    loan_record_head = loan_record.expr().hash()
    loans_root_hash = user_record.loans_root_hash or ZERO32
    loans_trie = RadixTree(
        root_hash=None if loans_root_hash == ZERO32 else loans_root_hash,
        contact=block.contact,
        root_block=user_record.loans_root_block,
    )
    if get_from_radix_tree(loans_trie, node, transaction_hash) is not None:
        return STATUS_FAILED

    put_in_radix_tree(loans_trie, node, transaction_hash, loan_record_head)
    loan_exprs, _ = resolve_inner_exprs(node, loan_record.expr(), contact=block.contact)
    user_record = replace(
        user_record,
        loans_root_hash=loans_trie.root_hash or ZERO32,
        loans_root_block=loans_trie.root_block,
    )
    updated_user_record_head = user_record.expr().hash()
    put_in_radix_tree(
        treasury_account.data,
        node,
        transaction.sender,
        updated_user_record_head,
    )
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    treasury_account.balance -= principal
    sender_account.balance += principal
    user_record_exprs, _ = resolve_inner_exprs(node, user_record.expr(), contact=block.contact)
    _extend_pending_exprs(
        block,
        loan_exprs + _trie_exprs(loans_trie) + user_record_exprs,
    )
    return STATUS_SUCCESS


def _handle_unsecured_borrow(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
    transaction_hash: bytes,
    sender_account: Any,
    treasury_account: Any,
    request: TreasuryBorrowRequest,
) -> int:
    """Open an uncapped unsecured loan and mint its principal.

    An unsecured loan needs no collateral and no third-party backing. The
    treasury mints the principal ``L``: the borrower is credited ``L`` and the
    treasury is not debited, so the supply level grows by ``L``. The measured
    per-interval insurance premium is the whole cost; it is recorded on the
    loan and collected with the service, never deducted at origination.

    Args:
        node: Storage node used to resolve and persist trie/expr data.
        block: The block currently being applied against.
        transaction: The decoded ``TREASURY_BORROW`` transaction. Its
            ``amount`` is the principal ``L``.
        transaction_hash: The 32-byte hash of *transaction*, used as the
            loan's key in the borrower's loans trie.
        sender_account: The borrower's account. It is credited ``L``.
        treasury_account: The ``TREASURY_ADDRESS`` account. Its ``data`` trie
            holds the borrower's record.
        request: The decoded borrow request; its type is `UNSECURED`.

    Returns:
        ``STATUS_SUCCESS`` after the loan is written and the principal is
        credited, or ``STATUS_FAILED`` when the rate, the principal, or the
        premium cannot price the loan.
    """
    rate_fraction = all_time_rate_fraction(block)
    if rate_fraction is None:
        return STATUS_FAILED

    principal = transaction.amount
    payment_amount = _per_block_payment(principal=principal, rate_fraction=rate_fraction)
    if payment_amount is None:
        return STATUS_FAILED

    creation_block_number = _current_height(block)

    borrower_record = load_user_record(node, treasury_account.data, transaction.sender, contact=block.contact)
    if borrower_record is None:
        borrower_record = TreasuryUserRecord()

    # The plan fixes the fee as ceil(L * p), not floor(L * p). The value is a
    # per-interval premium now: it is recorded on the loan and collected with
    # the service, not deducted from the principal at origination.
    insurance_interval_fee = _insurance_interval_fee(
        principal=principal,
        loaned=borrower_record.loaned,
        defaulted=borrower_record.defaulted,
        block=block,
    )
    if insurance_interval_fee is None:
        return STATUS_FAILED

    if principal <= insurance_interval_fee:
        return STATUS_FAILED

    loan_record = TreasuryLoanRecord(
        creation_block_number=creation_block_number,
        loan_type=LoanType.UNSECURED,
        discounted_amount=principal,
        payment_amount=payment_amount,
        paid_until_block=creation_block_number,
        insurance_interval_fee=insurance_interval_fee,
        missed_count=0,
        owner=TREASURY_ADDRESS,
    )
    loan_record_head = loan_record.expr().hash()

    loans_root_hash = borrower_record.loans_root_hash or ZERO32
    loans_trie = RadixTree(
        root_hash=None if loans_root_hash == ZERO32 else loans_root_hash,
        contact=block.contact,
        root_block=borrower_record.loans_root_block,
    )
    if get_from_radix_tree(loans_trie, node, transaction_hash) is not None:
        return STATUS_FAILED
    put_in_radix_tree(loans_trie, node, transaction_hash, loan_record_head)
    loan_exprs, _ = resolve_inner_exprs(node, loan_record.expr(), contact=block.contact)

    updated_borrower_record = replace(
        borrower_record,
        loans_root_hash=loans_trie.root_hash or ZERO32,
        loans_root_block=loans_trie.root_block,
        loaned=borrower_record.loaned + principal,
    )
    updated_borrower_record_head = updated_borrower_record.expr().hash()
    put_in_radix_tree(treasury_account.data, node, transaction.sender, updated_borrower_record_head)
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    borrower_record_exprs, _ = resolve_inner_exprs(node, updated_borrower_record.expr(), contact=block.contact)

    # The treasury mints the principal: credit the borrower and debit nothing.
    # The chain's total_balance is a level and a mint moves it.
    sender_account.balance += principal

    block.statistics = (block.statistics or BlockStatistics()).with_loan(principal)

    _extend_pending_exprs(
        block, loan_exprs + _trie_exprs(loans_trie) + borrower_record_exprs
    )
    return STATUS_SUCCESS


def handle_treasury_borrow(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
    transaction_hash: bytes,
    sender_account: Any,
    treasury_account: Any,
) -> int:
    if (
        transaction.recipient != TREASURY_ADDRESS
        or transaction.sender == TREASURY_ADDRESS
        or transaction.amount <= 0
        or treasury_account is None
    ):
        return STATUS_FAILED

    nodes = _data_nodes(transaction.data)
    if len(nodes) != 1:
        return STATUS_FAILED
    loan_type_node = nodes[0]
    if get_expr_builtin_tag(loan_type_node) != "int":
        return STATUS_FAILED
    try:
        loan_type = LoanType(loan_type_node.value)
    except ValueError:
        return STATUS_FAILED
    request = TreasuryBorrowRequest(loan_type=loan_type)

    if request.loan_type == LoanType.SECURED:
        return _handle_secured_borrow(
            node=node,
            block=block,
            transaction=transaction,
            transaction_hash=transaction_hash,
            sender_account=sender_account,
            treasury_account=treasury_account,
            request=request,
        )

    if request.loan_type == LoanType.UNSECURED:
        return _handle_unsecured_borrow(
            node=node,
            block=block,
            transaction=transaction,
            transaction_hash=transaction_hash,
            sender_account=sender_account,
            treasury_account=treasury_account,
            request=request,
        )

    return STATUS_FAILED
