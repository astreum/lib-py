from __future__ import annotations

from collections import ChainMap
from typing import Any

from astreum.storage.radix import RadixTree, get_radix_node_expr
from astreum.expression import Expr
from astreum.consensus.transaction.treasury.record import TreasuryLoanRecord
from astreum.expression.helpers import get_expr_builtin_tag


class _PendingNodes:
    """A node view that reads a block's pending exprs before storage.

    A block applies its transactions before it writes their exprs. A later
    transaction in the same block must still see what an earlier one wrote
    into the treasury tries, so this view answers a hash from the pending
    list first and falls back to the node.
    """

    def __init__(self, node: Any, exprs: list[Expr]):
        self._node = node
        self._by_hash = {expr.hash(): expr for expr in exprs or () if expr is not None}

    @property
    def hot_storage(self):
        """The node's hot storage with the pending exprs in front."""
        return ChainMap(self._by_hash, getattr(self._node, "hot_storage", {}))

    def __getattr__(self, name: str):
        """Delegate every other storage attribute to the wrapped node."""
        return getattr(self._node, name)


def _data_nodes(data) -> list:
    """Flatten a transaction's ``data`` link chain into its elements.

    Args:
        data: The head of the transaction's data chain.

    Returns:
        The chain's elements in order, as a list of ``Expr`` heads.
    """
    result = []
    current = data
    while current is not None and get_expr_builtin_tag(current) == "link":
        if current.head is not None:
            result.append(current.head)
        current = current.tail
    return result


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


def insurance_interval_fee(
    *,
    principal: int,
    loaned: int,
    defaulted: int,
    block: object,
) -> int | None:
    """Return the measured per-interval insurance premium for *principal*.

    One estimator prices every insured loan. The smoothed rate ``p`` blends
    the borrower's own history with the all-time global counters:

    ``p = (defaulted * gc + gd) / (loaned * gc + gl)``

    and the premium is ``ceil(principal * p)``. The all-time counters live
    in the block statistics: ``total_loan_count`` (``gc``),
    ``total_written_off`` (``gd``), and ``total_loaned`` (``gl``).

    Args:
        principal: The insured principal, ``L`` for a borrow or ``C`` for a
            claim-cost loan.
        loaned: The borrower's own cumulative unsecured principal.
        defaulted: The borrower's own cumulative write-off.
        block: The block whose global counters are read.

    Returns:
        The premium, or `None` when a counter is positive but the blend's
        denominator is not. With no global loans the premium is `0`.
    """
    stats = getattr(block, "statistics", None)
    gc = stats.total_loan_count if stats is not None else 0
    gd = stats.total_written_off if stats is not None else 0
    gl = stats.total_loaned if stats is not None else 0
    if gc <= 0:
        return 0
    denominator = loaned * gc + gl
    if denominator <= 0:
        return None
    numerator = principal * (defaulted * gc + gd)
    return (numerator + denominator - 1) // denominator


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
