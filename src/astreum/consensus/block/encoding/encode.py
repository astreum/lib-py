from __future__ import annotations

from astreum.expression import Expr, NIL, link, int_, bytes_, symbol
from astreum.consensus.models.block import Block


def _statistics_to_expr(statistics: list | None) -> Expr:
    """Encode block statistics into a linked-list S-expression.

    In memory, entry 0 is ``(fee, stake, claims, 0, 0, 0)`` (all-time
    cumulative, never rotates) and entry N is ``(prev_fee, prev_stake,
    curr_fee, curr_stake, prev_claims, curr_claims)`` for pow2-sized block
    windows.

    Encoded as:
    - entry 0: ``(fee, stake, claims)``
    - entry N: ``(prev_fee, prev_stake, curr_fee, curr_stake, prev_claims, curr_claims)``

    Args:
        statistics: The statistics list.

    Returns:
        The encoded expression.
    """
    if not statistics:
        return NIL
    expr = NIL
    for i in range(len(statistics) - 1, -1, -1):
        if i == 0:
            fee, stake, claims = statistics[i][:3]
            entry = link(int_(fee), link(int_(stake), link(int_(claims), NIL)))
        else:
            pf, ps, cf, cs, pc, cc = statistics[i]
            entry = link(int_(pf), link(int_(ps), link(int_(cf), link(int_(cs), link(int_(pc), link(int_(cc), NIL))))))
        expr = link(entry, expr)
    return expr


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
