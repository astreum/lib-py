from __future__ import annotations

from astreum.expression import Expr, NIL, link, int_, bytes_, symbol
from astreum.consensus.models.block import Block
from astreum.consensus.models.statistics import BlockStatistics


def _statistics_to_expr(statistics: BlockStatistics | None) -> Expr:
    """Encode block statistics into a linked-list S-expression.

    Statistics is a single-entry list that holds the fields in declaration
    order. Empty input encodes as NIL.

    Args:
        statistics: The statistics value.

    Returns:
        The encoded expression.
    """
    if statistics is None:
        return NIL
    values = (
        statistics.total_fees,
        statistics.total_balance,
        statistics.cumulative_total_balance,
        statistics.total_claims,
        statistics.total_tx_fees,
        statistics.total_tx_bytes,
        statistics.total_loan_count,
        statistics.total_loaned,
        statistics.total_written_off,
    )
    entry: Expr = NIL
    for value in reversed(values):
        entry = link(int_(value), entry)
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
    block.body_hash = body.hash()
    expr: Expr = link(
        link(body, link(bytes_(block.signature), NIL)),
        symbol("block"))
    return expr
