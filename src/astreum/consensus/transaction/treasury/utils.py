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


def _return_claimed_offer_limits(
    node,
    treasury_account,
    loan: TreasuryLoanRecord,
) -> list[Expr]:
    """Return each backing seller's proportional share of limit at loan-end.

    Called once a loan's ``next_payment_block_number`` reaches ``0``
    (whether via a clean payoff, a close, or a full write-off). For every
    ``(seller_address, offer_transaction_id, limit)`` this loan claimed at
    origination, credits that seller's ``sold_limit`` back by
    ``limit * paid_count // payment_count``, where ``paid_count =
    payment_count - loan.missed_count`` — the remainder stays in
    ``sold_limit`` permanently as the seller's loss on this loan. A no-op
    for `SECURED` loans (`claimed_offers` is always empty for those).

    Args:
        node: Storage node used to resolve and persist trie/expr data.
        treasury_account: The `TREASURY_ADDRESS` account; its ``data`` trie
            holds every seller's `TreasuryUserRecord`, keyed by seller
            address. Mutated in place (multiple sellers' slots may be
            written in one call); ``data_hash`` is refreshed before return.
        loan: The loan that just reached full payoff/close/write-off, with
            its *final* ``missed_count`` already applied.

    Returns:
        Pending exprs for every updated seller record, to be extended onto
        the caller's own pending-expr list.
    """
    if not loan.claimed_offers or loan.payment_count <= 0:
        return []

    paid_count = max(0, loan.payment_count - loan.missed_count)
    returned_by_seller: dict[bytes, int] = {}
    for seller_address, _offer_transaction_id, limit in loan.claimed_offers:
        returned = limit * paid_count // loan.payment_count
        returned_by_seller[seller_address] = returned_by_seller.get(seller_address, 0) + returned

    pending_exprs: list[Expr] = []
    for seller_address, returned in returned_by_seller.items():
        if returned <= 0:
            continue
        seller_record_head = get_from_radix_tree(treasury_account.data, node, seller_address)
        seller_record = TreasuryUserRecord.from_storage(node, seller_record_head or ZERO32)
        if seller_record is None:
            continue
        updated_seller_record = replace(
            seller_record,
            sold_limit=max(0, seller_record.sold_limit - returned),
        )
        updated_head = updated_seller_record.expr().hash()
        put_in_radix_tree(treasury_account.data, node, seller_address, updated_head)
        seller_exprs, _ = resolve_inner_exprs(node, updated_seller_record.expr())
        pending_exprs.extend(seller_exprs)

    if pending_exprs:
        treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    return pending_exprs
