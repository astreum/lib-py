from __future__ import annotations

from dataclasses import replace
from typing import Any

from astreum.expression import Expr, resolve_inner_exprs
from astreum.expression import ZERO32
from astreum.storage.radix import RadixTree, get_all_from_radix_tree_with_blocks, get_from_radix_tree, put_in_radix_tree
from astreum.consensus.account import create_account
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.transaction.treasury.record import (
    load_loan_record,
    load_user_record,
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
)
from astreum.consensus.transaction.treasury.utils import (
    _PendingNodes,
    _overdue_blocks,
    _trie_exprs,
)


def load_user_loan(
    *,
    node: Any,
    treasury_account: Any,
    borrower: bytes,
    loan_transaction_id: bytes,
    contact=None,
) -> TreasuryLoanRecord | None:
    """Load one loan from a borrower's treasury user record.

    Every loan type, including `CLAIM_COST`, is stored in the same loans
    trie, keyed by the transaction that created it.

    Args:
        node: Storage node used to resolve trie and expr data.
        treasury_account: The `TREASURY_ADDRESS` account. Its ``data`` trie
            holds the borrower's `TreasuryUserRecord`.
        borrower: The borrower's 32-byte address.
        loan_transaction_id: The loan's trie key.
        contact: Optional node contact for hash-only reads.

    Returns:
        The loan, or None if the borrower has no record, has no loans, or
        the key is absent.
    """
    user_record = load_user_record(
        node, treasury_account.data, borrower, contact=contact
    )
    if user_record is None or user_record.loans_root_hash == ZERO32:
        return None
    loans_trie = RadixTree(
        root_hash=user_record.loans_root_hash,
        contact=contact,
        root_block=user_record.loans_root_block,
    )
    return load_loan_record(node, loans_trie, loan_transaction_id, contact=contact)


def load_user_loans(
    *,
    node: Any,
    treasury_account: Any,
    borrower: bytes,
    user_record: TreasuryUserRecord | None = None,
    contact=None,
) -> list[tuple[bytes, TreasuryLoanRecord]]:
    """Load every loan in a borrower's treasury user record.

    Every loan type lives in the same loans trie, keyed by the transaction
    that created it, so one enumeration covers `SECURED`, `UNSECURED`, and
    `CLAIM_COST` loans.

    Args:
        node: Storage node used to resolve trie and expr data.
        treasury_account: The `TREASURY_ADDRESS` account. Its ``data`` trie
            holds the borrower's `TreasuryUserRecord`.
        borrower: The borrower's 32-byte address.
        user_record: The borrower's current record, or None to load it from
            the treasury data trie.
        contact: Optional node contact for hash-only reads.

    Returns:
        ``(loan_transaction_id, loan)`` pairs, empty if the borrower has no
        record, has no loans, or a loan cannot be decoded.
    """
    if user_record is None:
        user_record = load_user_record(
            node, treasury_account.data, borrower, contact=contact
        )
    if user_record is None or user_record.loans_root_hash == ZERO32:
        return []
    loans_trie = RadixTree(
        root_hash=user_record.loans_root_hash,
        contact=contact,
        root_block=user_record.loans_root_block,
    )
    loans: list[tuple[bytes, TreasuryLoanRecord]] = []
    for loan_transaction_id, (loan_head, loan_block) in (
        get_all_from_radix_tree_with_blocks(loans_trie, node).items()
    ):
        loan = TreasuryLoanRecord.from_storage(
            node, loan_head, contact=contact, block_hash=loan_block
        )
        if loan is not None:
            loans.append((loan_transaction_id, loan))
    return loans


def write_user_loan(
    *,
    node: Any,
    treasury_account: Any,
    borrower: bytes,
    user_record: TreasuryUserRecord | None,
    loan_transaction_id: bytes,
    loan: TreasuryLoanRecord,
    pending_exprs: list[Expr],
    contact=None,
) -> TreasuryUserRecord | None:
    """Store one loan in a borrower's treasury user record.

    The loan goes into the borrower's loans trie, keyed by
    *loan_transaction_id* like every other loan. The user record's loans
    root is updated and written back to the treasury data trie.

    Args:
        node: Storage node used to resolve and persist trie/expr data.
        treasury_account: The `TREASURY_ADDRESS` account. Its ``data`` trie
            receives the updated user record.
        borrower: The borrower's 32-byte address.
        user_record: The borrower's current user record, or None to load it
            from the treasury data trie. A default record is created only
            when storage has none.
        loan_transaction_id: The loan's trie key.
        loan: The loan record to store.
        pending_exprs: Pending expressions to extend with the loan, the
            loans trie nodes, and the updated user record. They are also read
            back, so a record written by an earlier transaction of the same
            block is visible.
        contact: Optional node contact for hash-only reads.

    Returns:
        The updated user record, or None if the key already exists.
    """
    # Earlier transactions of this block may have written the record and the
    # loans trie; their exprs are pending, not stored yet.
    lookup = _PendingNodes(node, pending_exprs)
    if user_record is None:
        user_record = load_user_record(
            lookup, treasury_account.data, borrower, contact=contact
        )
    if user_record is None:
        user_record = TreasuryUserRecord()
    loans_root_hash = user_record.loans_root_hash or ZERO32
    loans_trie = RadixTree(
        root_hash=None if loans_root_hash == ZERO32 else loans_root_hash,
        contact=contact,
        root_block=user_record.loans_root_block,
    )
    if get_from_radix_tree(loans_trie, lookup, loan_transaction_id) is not None:
        return None

    loan_expr = loan.expr()
    put_in_radix_tree(loans_trie, lookup, loan_transaction_id, loan_expr.hash())
    updated_user_record = replace(
        user_record,
        loans_root_hash=loans_trie.root_hash or ZERO32,
        loans_root_block=loans_trie.root_block,
    )
    put_in_radix_tree(
        treasury_account.data,
        lookup,
        borrower,
        updated_user_record.expr().hash(),
    )
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32

    trie_exprs = _trie_exprs(loans_trie)
    # The new record names the new loans root, whose nodes are pending only.
    resolved = _PendingNodes(node, list(pending_exprs) + trie_exprs + [loan_expr])
    loan_exprs, _ = resolve_inner_exprs(resolved, loan_expr, contact=contact)
    user_record_exprs, _ = resolve_inner_exprs(
        resolved, updated_user_record.expr(), contact=contact
    )
    pending_exprs.extend(loan_exprs + trie_exprs + user_record_exprs)
    return updated_user_record


def settle_claim_payout(
    *,
    node: Any,
    block: Any,
    accounts: Any,
    treasury_account: Any,
    borrower: bytes,
    payout: int,
) -> int:
    """Net a claim payout against every accrued loan service of the borrower.

    Sums ``overdue_blocks * payment_amount`` over every open loan in the
    borrower's loans trie, using the canonical ``paid_until_block``
    convention. When the payout covers the total, every owner receives its
    loan's service, every settled loan's ``paid_until_block`` advances to the
    current height, and the sender keeps the remainder. An insured loan also
    pays one ``insurance_interval_fee`` per settled block, taken from the
    payout after the service and credited to the treasury as income; the owner
    still receives the whole service. When the payout does not cover the
    service and the premiums, nothing is settled and no owner is paid: the
    sender keeps the whole payout, and the accrued service stays owed on the
    loans for the owner to collect later.

    A prepaid loan (``paid_until_block`` above the current height) has no
    accrued service and keeps its pointer. A closed loan
    (``paid_until_block == 0``) is not settled. A loan whose pointer is below
    its ``creation_block_number`` is malformed and is skipped, like the repay
    handlers' defensive guard.

    Args:
        node: Storage node used to resolve and persist trie/expr data.
        block: The block whose height, contact, and pending exprs apply.
        accounts: The block's accounts cache. Owner balances are credited
            through it so supply tracking sees the movement.
        treasury_account: The `TREASURY_ADDRESS` account. Its ``data`` trie
            holds the borrower's `TreasuryUserRecord`; its balance receives
            the premium income.
        borrower: The borrower's 32-byte address.
        payout: The claim's gross payout ``R``.

    Returns:
        The amount to credit to the sender: ``payout - service - premiums``
        when the payout covers both, possibly ``0``; otherwise the whole
        ``payout``.
    """
    current_height = int(block.height)
    # Earlier transactions of this block may have written the loans without
    # flushing their exprs; read them from the pending list first.
    lookup = _PendingNodes(node, block.pending_exprs)
    user_record = load_user_record(
        lookup, treasury_account.data, borrower, contact=block.contact
    )
    if user_record is None or user_record.loans_root_hash == ZERO32:
        return payout

    overdue_loans: list[tuple[bytes, TreasuryLoanRecord, int, int]] = []
    service_total = 0
    premium_total = 0
    for loan_transaction_id, loan in load_user_loans(
        node=lookup,
        treasury_account=treasury_account,
        borrower=borrower,
        user_record=user_record,
        contact=block.contact,
    ):
        if loan.paid_until_block <= 0 or loan.payment_amount <= 0:
            continue
        if loan.paid_until_block < loan.creation_block_number:
            continue
        overdue_blocks = _overdue_blocks(
            loan=loan, current_height=current_height
        )
        if overdue_blocks <= 0:
            continue
        service = overdue_blocks * loan.payment_amount
        premium = (
            overdue_blocks * loan.insurance_interval_fee
            if loan.loan_type != LoanType.SECURED
            else 0
        )
        overdue_loans.append((loan_transaction_id, loan, service, premium))
        service_total += service
        premium_total += premium

    if service_total == 0:
        return payout
    if payout < service_total + premium_total:
        # The rent is the provider's own money: it keeps the whole payout.
        # Nothing is settled, so every pointer stays and the accrued service
        # and premium remain owed on the loans, to be collected later.
        return payout

    loans_trie = RadixTree(
        root_hash=user_record.loans_root_hash,
        contact=block.contact,
        root_block=user_record.loans_root_block,
    )
    loan_exprs: list[Expr] = []
    for loan_transaction_id, loan, service, _premium in overdue_loans:
        updated_loan = replace(loan, paid_until_block=current_height)
        put_in_radix_tree(
            loans_trie, lookup, loan_transaction_id, updated_loan.expr().hash()
        )
        resolved_loan, _ = resolve_inner_exprs(
            lookup, updated_loan.expr(), contact=block.contact
        )
        loan_exprs.extend(resolved_loan)

        if loan.owner == TREASURY_ADDRESS:
            treasury_account.balance += service
        else:
            owner_account = accounts.get_account(loan.owner, node)
            if owner_account is None:
                owner_account = create_account()
            owner_account.balance += service
            accounts.set_account(loan.owner, owner_account)

    updated_user_record = replace(
        user_record,
        loans_root_hash=loans_trie.root_hash or ZERO32,
        loans_root_block=loans_trie.root_block,
        total_interest_paid=user_record.total_interest_paid + service_total,
    )
    put_in_radix_tree(
        treasury_account.data,
        lookup,
        borrower,
        updated_user_record.expr().hash(),
    )
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    # The premium rides with the service: the owner keeps the whole service
    # and the treasury takes one interval premium per settled block from the
    # payout as income. There is no fund.
    if premium_total:
        treasury_account.balance += premium_total
    accounts.set_account(TREASURY_ADDRESS, treasury_account)

    trie_exprs = _trie_exprs(loans_trie)
    # The new record names the new loans root, whose nodes and loans are
    # pending only.
    resolved = _PendingNodes(node, list(block.pending_exprs) + trie_exprs + loan_exprs)
    user_record_exprs, _ = resolve_inner_exprs(
        resolved, updated_user_record.expr(), contact=block.contact
    )
    block.pending_exprs.extend(loan_exprs + trie_exprs + user_record_exprs)
    return payout - service_total - premium_total


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
