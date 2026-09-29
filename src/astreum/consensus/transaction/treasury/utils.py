from __future__ import annotations

from dataclasses import replace

from astreum.storage.radix import RadixTree, get_from_radix_tree, get_radix_node_expr, put_in_radix_tree
from astreum.expression import Expr, ZERO32, resolve_inner_exprs
from astreum.consensus.transaction.treasury.record import TreasuryLoanRecord, TreasuryUserRecord


def _collect_sub_exprs(expr: Expr) -> list:
    """Walk an expr tree and collect all sub-expressions without resolving hashes."""
    result = [expr]
    if expr._tag == "link":
        if expr._head is not None:
            result.extend(_collect_sub_exprs(expr._head))
        if expr._tail is not None:
            result.extend(_collect_sub_exprs(expr._tail))
    return result


def _trie_exprs(trie: RadixTree) -> list:
    emitted: list = []
    if not trie.nodes:
        return emitted
    for node_hash in sorted(trie.nodes.keys()):
        trie_node = trie.nodes[node_hash]
        expr = get_radix_node_expr(trie_node)
        if expr.hash() != node_hash:
            continue
        emitted.extend(_collect_sub_exprs(expr))
    return emitted


def _paid_payment_count(loan: TreasuryLoanRecord) -> int | None:
    if loan.next_payment_block_number == 0:
        return loan.payment_count
    if loan.payment_interval_blocks <= 0:
        return None
    paid_span = loan.next_payment_block_number - loan.creation_block_number
    if paid_span <= 0 or paid_span % loan.payment_interval_blocks != 0:
        return None
    return (paid_span // loan.payment_interval_blocks) - 1


def _remaining_payment_count(loan: TreasuryLoanRecord) -> int | None:
    total_payment_count = loan.payment_count
    paid_payment_count = _paid_payment_count(loan)
    if total_payment_count is None or paid_payment_count is None:
        return None
    remaining = total_payment_count - paid_payment_count
    if remaining < 0:
        return None
    return remaining


def _interest_paid_delta(
    *,
    loan: TreasuryLoanRecord,
    paid_before: int,
    paid_after: int,
    total_payment_count: int,
) -> int | None:
    scheduled_total = loan.payment_amount * total_payment_count
    total_interest = scheduled_total - loan.discounted_amount
    if total_interest < 0:
        return None
    interest_before = total_interest * paid_before // total_payment_count
    interest_after = total_interest * paid_after // total_payment_count
    return interest_after - interest_before


def _release_guarantees(
    node,
    treasury_account,
    loan: TreasuryLoanRecord,
) -> list[Expr]:
    """Return each backing guarantor's proportional share of guarantee at loan-end.

    Called once a loan's ``next_payment_block_number`` reaches ``0``
    (whether via a clean payoff, a close, or a full write-off). For every
    ``(guarantor_address, guarantee_transaction_id, guarantee_amount)`` this loan claimed at
    origination, credits that guarantor's ``guaranteed`` back by
    ``guarantee_amount * paid_count // payment_count``, where ``paid_count =
    payment_count - loan.missed_count`` — the remainder stays in
    ``guaranteed`` permanently as the guarantor's loss on this loan. A no-op
    for `SECURED` loans (`claimed_guarantees` is always empty for those).

    Args:
        node: Storage node used to resolve and persist trie/expr data.
        treasury_account: The `TREASURY_ADDRESS` account; its ``data`` trie
            holds every guarantor's `TreasuryUserRecord`, keyed by guarantor
            address. Mutated in place (multiple guarantors' slots may be
            written in one call); ``data_hash`` is refreshed before return.
        loan: The loan that just reached full payoff/close/write-off, with
            its *final* ``missed_count`` already applied.

    Returns:
        Pending exprs for every updated guarantor record, to be extended onto
        the caller's own pending-expr list.
    """
    if not loan.claimed_guarantees or loan.payment_count <= 0:
        return []

    paid_count = max(0, loan.payment_count - loan.missed_count)
    returned_by_guarantor: dict[bytes, int] = {}
    for guarantor_address, _guarantee_transaction_id, guarantee_amount in loan.claimed_guarantees:
        returned = guarantee_amount * paid_count // loan.payment_count
        returned_by_guarantor[guarantor_address] = returned_by_guarantor.get(guarantor_address, 0) + returned

    pending_exprs: list[Expr] = []
    for guarantor_address, returned in returned_by_guarantor.items():
        if returned <= 0:
            continue
        guarantor_record_head = get_from_radix_tree(treasury_account.data, node, guarantor_address)
        guarantor_record = TreasuryUserRecord.from_storage(node, guarantor_record_head or ZERO32)
        if guarantor_record is None:
            continue
        updated_guarantor_record = replace(
            guarantor_record,
            guaranteed=max(0, guarantor_record.guaranteed - returned),
        )
        updated_head = updated_guarantor_record.expr().hash()
        put_in_radix_tree(treasury_account.data, node, guarantor_address, updated_head)
        guarantor_exprs, _ = resolve_inner_exprs(node, updated_guarantor_record.expr())
        pending_exprs.extend(guarantor_exprs)

    if pending_exprs:
        treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    return pending_exprs
