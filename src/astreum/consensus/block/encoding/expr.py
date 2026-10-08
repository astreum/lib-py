from __future__ import annotations

from astreum.expression import Expr
from astreum.consensus.models.block import Block


def get_block_expr(block: Block) -> Expr:
    """Return the canonical S-expression for a Block.

    Caches the result on ``block._expr`` and returns it directly on
    subsequent calls rather than re-building.

    Args:
        block: The Block to serialize.

    Returns:
        The Expr representing the serialized block.
    """
    if block._expr is not None:
        return block._expr
    from astreum.consensus.block.encoding.encode import block_to_expr

    block._expr = block_to_expr(block)
    return block._expr
