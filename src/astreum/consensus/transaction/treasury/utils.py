from __future__ import annotations

from dataclasses import replace

from astreum.storage.radix import RadixTree, get_radix_node_expr, put_in_radix_tree
from astreum.expression import Expr, ZERO32, resolve_inner_exprs
from astreum.consensus.transaction.treasury.record import TreasuryLoanRecord, load_user_record
from astreum.expression.helpers import get_expr_builtin_tag


def _collect_sub_exprs(expr: Expr) -> list:
    """Walk an expr tree and collect all sub-expressions without resolving hashes.

    Args:
        expr: The expression to walk.

    Returns:
        List of all expressions.
    """
    result = [expr]
    if get_expr_builtin_tag(expr) == "link":
        if expr.head is not None:
            result.extend(_collect_sub_exprs(expr.head))
        if expr.tail is not None:
            result.extend(_collect_sub_exprs(expr.tail))
    return result


def _trie_exprs(trie: RadixTree) -> list:
    """Extract all expressions from a radix trie.

    Args:
        trie: The radix trie.

    Returns:
        List of expressions.
    """
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


def _overdue_blocks(*, loan: TreasuryLoanRecord, current_height: int) -> int:
    """Return the number of unpaid blocks after the settled pointer.

    Convention: ``paid_until_block`` is the last block whose payment is
    settled. Every block after it is due, so at
    ``paid_until_block == current_height`` the loan is exactly current
    (zero overdue), and at ``paid_until_block == current_height - 1`` it
    has one block of arrears. A prepaid pointer (greater than
    ``current_height``) has no arrears. This is the plan's
    ``max(0, current_height - paid_until_block)``.

    Args:
        loan: The open loan record.
        current_height: The block height the transaction runs in.

    Returns:
        The count of overdue blocks, never negative.
    """
    return max(0, current_height - loan.paid_until_block)


def _release_guarantees(
    node,
    treasury_account,
    loan: TreasuryLoanRecord,
) -> list[Expr]:
    """Release a closed loan's backing guarantees, net of its written-off losses.

    Called once a loan's ``paid_until_block`` reaches ``0``. The guarantee
    covers the loan's arrears up to ``guarantee.amount``. The loan's
    permanent loss is its written-off arrears,
    ``loan.missed_count * loan.payment_amount``, capped at the total of the
    guarantees backing it. That loss is split across the claimed guarantees in
    proportion to their amounts. The loss share floors under integer division,
    so the few-unit remainder of the write-off is released too and the
    retained loss can end slightly below the written-off amount. Each
    guarantor's ``guaranteed`` is reduced by its released amount, so:

    - no write-off (a clean close) releases the full guarantee;
    - a partial write-off releases the guarantee minus its share of the loss;
    - a write-off at or above the total guarantee releases nothing.

    A no-op for `SECURED` loans (`claimed_guarantees` is always empty for
    those).

    Args:
        node: Storage node used to resolve and persist trie/expr data.
        treasury_account: The `TREASURY_ADDRESS` account. Its ``data`` trie
            holds every guarantor's `TreasuryUserRecord`, keyed by guarantor
            address. Mutated in place (multiple guarantors' slots may be
            written in one call). ``data_hash`` is refreshed before return.
        loan: The loan that just closed, with its *final* ``missed_count``
            already applied.

    Returns:
        Pending exprs for every updated guarantor record, to be extended onto
        the caller's own pending-expr list.
    """
    if not loan.claimed_guarantees:
        return []

    total_guarantee = sum(
        guarantee_amount
        for _guarantor_address, _guarantee_transaction_id, guarantee_amount
        in loan.claimed_guarantees
    )
    if total_guarantee <= 0:
        return []

    written_off = max(0, loan.missed_count) * max(0, loan.payment_amount)
    lost_total = min(total_guarantee, written_off)

    returned_by_guarantor: dict[bytes, int] = {}
    for guarantor_address, _guarantee_transaction_id, guarantee_amount in loan.claimed_guarantees:
        loss = lost_total * guarantee_amount // total_guarantee
        returned = guarantee_amount - loss
        returned_by_guarantor[guarantor_address] = returned_by_guarantor.get(guarantor_address, 0) + returned

    pending_exprs: list[Expr] = []
    for guarantor_address, returned in returned_by_guarantor.items():
        if returned <= 0:
            continue
        guarantor_record = load_user_record(node, treasury_account.data, guarantor_address, contact=treasury_account.data.contact)
        if guarantor_record is None:
            continue
        updated_guarantor_record = replace(
            guarantor_record,
            guaranteed=max(0, guarantor_record.guaranteed - returned),
        )
        updated_head = updated_guarantor_record.expr().hash()
        put_in_radix_tree(treasury_account.data, node, guarantor_address, updated_head)
        guarantor_exprs, _ = resolve_inner_exprs(node, updated_guarantor_record.expr(), contact=treasury_account.data.contact)
        pending_exprs.extend(guarantor_exprs)

    if pending_exprs:
        treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    return pending_exprs
