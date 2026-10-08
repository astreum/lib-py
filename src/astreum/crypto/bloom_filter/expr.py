from __future__ import annotations

from astreum.expression import Expr, NIL, link, int_, bytes_
from astreum.crypto.bloom_filter.main import BloomFilter


def bloom_to_expr(bf: BloomFilter) -> Expr:
    """
    Serialize a BloomFilter to an Expr.

    Args:
        bf: Bloom filter to serialize.
    """
    tiers: Expr = NIL
    for tier in reversed(bf.tiers):
        tiers = link(bytes_(bytes(tier)), tiers)

    if bf.start_hash is None:
        return link(
            NIL,
            link(
                int_(bf.count),
                tiers,
            ),
        )
    else:
        return Expr("link",
            head_hash=bf.start_hash,
            tail=link(
                int_(bf.count),
                tiers,
            ),
        )


def bloom_from_expr(expr: Expr) -> BloomFilter:
    """
    Deserialize a BloomFilter from an Expr.

    Args:
        expr: Expression to deserialize.
    """
    head = expr._head
    if head is None or head is NIL:
        start_hash = None
    else:
        start_hash = expr._head_hash

    count = expr._tail._head.value

    tiers: list[bytearray] = []
    current = expr._tail._tail
    while current._tag == "link" and current is not NIL:
        tiers.append(bytearray(current._head.value))
        current = current._tail
    if current._tag == "bytes":
        tiers.append(bytearray(current.value))

    bf = BloomFilter()
    bf.count = count
    bf.tiers = tiers
    bf.start_hash = start_hash
    return bf
