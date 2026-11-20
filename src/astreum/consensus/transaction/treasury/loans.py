from __future__ import annotations

from dataclasses import replace
from typing import Any

from astreum.expression import Expr, resolve_inner_exprs
from astreum.expression import ZERO32
from astreum.storage.radix import RadixTree, get_all_from_radix_tree_with_blocks, get_from_radix_tree, put_in_radix_tree
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.transaction.treasury.record import (
    load_user_record,
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
)
from astreum.consensus.transaction.treasury.utils import _trie_exprs


def _apply_treasury_loan_payment(
    *,
    node: Any,
    pending_exprs: list[Expr],
    treasury_account: Any,
    accounts: Any,
    borrower: bytes,
    loans_trie: RadixTree,
    user_record: TreasuryUserRecord,
    loan_transaction_id: bytes,
    loan: TreasuryLoanRecord,
    amount: int,
) -> TreasuryUserRecord | None:
    """Apply one whole-block payment to an open perpetual loan.

    The pointer moves by ``amount // payment_amount`` blocks, exactly as in
    ``TREASURY_REPAY``. The stake-return path only feeds `SECURED` loans, so
    there is no write-off branch here.

    Args:
        node: Storage node used to resolve and persist trie/expr data.
        pending_exprs: Pending expressions to extend.
        treasury_account: The `TREASURY_ADDRESS` account.
        accounts: The block's accounts cache.
        borrower: The borrower's 32-byte address.
        loans_trie: The borrower's loans trie.
        user_record: The borrower's treasury user record.
        loan_transaction_id: The loan's trie key.
        loan: The open loan record.
        amount: The value to settle, a whole multiple of ``payment_amount``.

    Returns:
        The updated user record, or None if the payment cannot apply.
    """
    if amount <= 0 or loan.payment_amount <= 0:
        return None
    if loan.paid_until_block == 0:
        return None
    if amount % loan.payment_amount != 0:
        return None
    if loan.paid_until_block < loan.creation_block_number:
        return None

    blocks_paid = amount // loan.payment_amount
    paid_until_block = loan.paid_until_block + blocks_paid

    if loan.owner == TREASURY_ADDRESS:
        treasury_account.balance += amount
    else:
        owner_account = accounts.get_account(loan.owner, node)
        if owner_account is None:
            return None
        owner_account.balance += amount
        accounts.set_account(loan.owner, owner_account)

    updated_loan = replace(loan, paid_until_block=paid_until_block)
    updated_loan_head = updated_loan.expr().hash()
    put_in_radix_tree(loans_trie, node, loan_transaction_id, updated_loan_head)
    loan_exprs, _ = resolve_inner_exprs(node, updated_loan.expr(), contact=accounts.contact)

    updated_user_record = replace(
        user_record,
        loans_root_hash=loans_trie.root_hash or ZERO32,
        loans_root_block=loans_trie.root_block,
        total_interest_paid=user_record.total_interest_paid + amount,
    )
    updated_user_record_head = updated_user_record.expr().hash()

    put_in_radix_tree(treasury_account.data, node, borrower, updated_user_record_head)
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    user_record_exprs, _ = resolve_inner_exprs(node, updated_user_record.expr(), contact=accounts.contact)
    pending_exprs.extend(
        loan_exprs + _trie_exprs(loans_trie) + user_record_exprs
    )
    return updated_user_record


def apply_treasury_loan_payments_from_stake_return(
    *,
    node: Any,
    accounts: Any,
    borrower: bytes,
    amount: int,
) -> bool:
    """Apply stake-return value to active loans, leaving any remainder staked."""
    if amount <= 0:
        return False

    treasury_account = accounts.get_account(TREASURY_ADDRESS, node)
    if treasury_account is None:
        return False

    user_record = load_user_record(node, treasury_account.data, borrower, contact=accounts.contact)
    if user_record is None or user_record.balance < amount:
        return False
    if user_record.loans_root_hash == ZERO32:
        return True

    loans_trie = RadixTree(
        root_hash=user_record.loans_root_hash,
        contact=accounts.contact,
        root_block=user_record.loans_root_block,
    )
    active_loans: list[tuple[int, bytes, TreasuryLoanRecord]] = []
    for loan_transaction_id, (loan_record_head, loan_block) in get_all_from_radix_tree_with_blocks(
        loans_trie, node
    ).items():
        loan = TreasuryLoanRecord.from_storage(
            node, loan_record_head, contact=accounts.contact, block_hash=loan_block
        )
        if (
            loan is None
            or loan.loan_type != LoanType.SECURED
            or loan.paid_until_block == 0
        ):
            continue
        active_loans.append(
            (loan.paid_until_block, loan_transaction_id, loan)
        )

    if not active_loans:
        return True

    available_amount = amount
    applied_amount = 0
    current_user_record = user_record
    ordered_loans = [
        (loan_transaction_id, loan)
        for _, loan_transaction_id, loan in sorted(active_loans)
    ]
    while available_amount > 0 and ordered_loans:
        applied_this_pass = False
        next_pass_loans: list[tuple[bytes, TreasuryLoanRecord]] = []
        for loan_transaction_id, loan in ordered_loans:
            if loan.payment_amount <= 0:
                continue
            if available_amount < loan.payment_amount:
                next_pass_loans.append((loan_transaction_id, loan))
                continue

            next_balance = current_user_record.balance - loan.payment_amount
            if next_balance < 0:
                return False

            updated_user_record = _apply_treasury_loan_payment(
                node=node,
                pending_exprs=accounts.pending_exprs,
                treasury_account=treasury_account,
                accounts=accounts,
                borrower=borrower,
                loans_trie=loans_trie,
                user_record=replace(current_user_record, balance=next_balance),
                loan_transaction_id=loan_transaction_id,
                loan=loan,
                amount=loan.payment_amount,
            )
            if updated_user_record is None:
                return False

            current_user_record = updated_user_record
            available_amount -= loan.payment_amount
            applied_amount += loan.payment_amount
            applied_this_pass = True

            # Each applied payment settles exactly one block.
            next_pass_loans.append(
                (
                    loan_transaction_id,
                    replace(loan, paid_until_block=loan.paid_until_block + 1),
                )
            )

        if not applied_this_pass:
            break
        ordered_loans = next_pass_loans

    if applied_amount == 0:
        return True

    accounts.set_account(TREASURY_ADDRESS, treasury_account)
    return True
