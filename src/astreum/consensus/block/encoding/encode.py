from __future__ import annotations

from astreum.expression import Expr, NIL, link, int_, bytes_, symbol
from astreum.consensus.models.block import Block


def _statistics_to_expr(statistics: list | None) -> Expr:
    """Encode block statistics into a linked-list S-expression.

    Statistics is a single-entry list containing the 6-tuple
    ``(total_fees, total_balance, cumulative_total_balance,
    total_claims, total_tx_fees, total_tx_bytes)``. Empty input encodes as NIL.

    Args:
        statistics: The statistics list.

    Returns:
        The encoded expression.

    Raises:
        ValueError: If statistics contains more than one entry.
    """
    if not statistics:
        return NIL
    if len(statistics) > 1:
        raise ValueError("statistics must contain at most one entry")
    fee, total_balance, cumulative_total_balance, claims, tx_fees, tx_bytes = statistics[0]
    entry = link(
        int_(fee),
        link(
            int_(total_balance),
            link(
                int_(cumulative_total_balance),
                link(
                    int_(claims),
                    link(int_(tx_fees), link(int_(tx_bytes), NIL)),
                ),
            ),
        ),
    )
    return link(entry, NIL)


def block_to_expr(block: Block) -> Expr:
    """Serialize a Block into its canonical S-expression.

    Builds the full block expression from the Block's fields including
    statistics, header chain fields, and signature, and assigns the
    resulting body hash to `block.body_hash`.

    Args:
        block: The Block to serialize.

    Returns:
        The Expr representing the serialized block.
    """
    if block._expr is not None:
        return block._expr
    body: Expr = link(_statistics_to_expr(block.statistics), NIL)
    body = link(bytes_(block.validator_public_key_bytes), body)
    body = link(Expr("link", head_hash=block.transactions_hash), body)
    body = link(int_(block.transaction_count), body)
    body = link(int_(block.total_base_fee), body)
    body = link(int_(block.total_transaction_fee), body)
    body = link(int_(block.total_storage_fee), body)
    body = link(int_(block.timestamp), body)
    body = link(int_(block.storage_difficulty), body)
    body = link(Expr("link", head_hash=block.receipts_hash), body)
    body = link(Expr("link", head_hash=block.previous_era_hash), body)
    body = link(Expr("link", head_hash=block.previous_block_hash), body)
    body = link(int_(block.nonce or 0), body)
    body = link(int_(block.height), body)
    body = link(int_(block.validation_difficulty), body)
    body = link(int_(block.chain_id), body)
    body = link(Expr("link", head_hash=block.bloom_hash), body)
    body = link(Expr("link", head_hash=block.accounts_hash), body)
    body = link(int_(block.global_loan_count), body)
    body = link(int_(block.global_defaulted), body)
    body = link(int_(block.global_loaned), body)
    block.body_hash = body.hash()
    expr: Expr = link(
        link(body, link(bytes_(block.signature), NIL)),
        symbol("block"))
    return expr
