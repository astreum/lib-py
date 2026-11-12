"""User-facing query API for the Astreum chain.

Usage::

    from astreum import get_block, find_transactions
    from astreum.crypto.bloom_search import make_search_variants
    from astreum.expression import ZERO32

    # ``sender`` is a 32-byte address; ``ZERO32`` means "match any".
    keys = make_search_variants(ZERO32, sender, ZERO32)
    block = get_block(node, height=5000)
    for tx, matched_keys in find_transactions(node, keys, limit=10):
        ...
"""

from __future__ import annotations

from typing import Iterator, Optional, TYPE_CHECKING

from astreum.crypto.bloom_search.search import iter_matching_transactions

if TYPE_CHECKING:
    from astreum.consensus.models.block import Block
    from astreum.consensus.models.receipt import Receipt
    from astreum.consensus.transaction.model import Transaction


def get_block(node, *, height: int) -> Optional["Block"]:
    """Fetch a block by its chain height.

    Returns ``None`` if the block hasn't been mined yet or is not reachable
    from the node's current tip.

    Args:
        node: Node instance to query.
        height: Chain height of the target block.

    Returns:
        Optional['Block']: Description of the return value.
    """
    from astreum.crypto.bloom_search.block_search import find_block_by_height

    return find_block_by_height(
        node,
        starting_block=node.latest_block,
        target_height=height,
    )


def find_transactions(
    node,
    keys: list[bytes],
    *,
    start_height: Optional[int] = None,
    end_height: int = 0,
    limit: int = 1,
) -> Iterator[tuple["Transaction", list[bytes]]]:
    """Yield transactions that match any of ``keys``, newest first.

    Each key has its own result budget. ``limit == 0`` makes every key
    unlimited.

    ``start_height`` — search backward from this height (default: the
    node's latest block). If the height is not reachable, nothing is
    yielded.

    ``end_height`` — stop searching when blocks drop below this height
    (default: ``0`` — search the entire chain).

    Args:
        node: Node instance to query.
        keys: Candidate 160-byte search keys.
        start_height: Height to start searching backward from.
        end_height: Height to stop searching at.
        limit: Maximum results per key (0 means unlimited).

    Yields:
        tuple["Transaction", list[bytes]]: A matching transaction and the
        subset of ``keys`` it matched. ``matched_keys`` lists the matched
        keys that were still under their per-key limit; a key already at
        its limit is not reported again.
    """
    from astreum.crypto.bloom_search.block_search import find_block_by_height

    if start_height is None:
        start_block = node.latest_block
    else:
        start_block = find_block_by_height(
            node,
            starting_block=node.latest_block,
            target_height=start_height,
        )
        if start_block is None:
            return

    yield from iter_matching_transactions(
        node,
        keys=keys,
        starting_block=start_block,
        end_block_height=end_height,
        limit=limit,
    )


def get_receipt(node, block_hash: bytes, tx_hash: bytes) -> Optional["Receipt"]:
    """Fetch the receipt of a transaction in a block.

    ``None`` means "not available now". It does not tell whether the
    receipt will ever exist. It is returned when the block has no
    receipts, when the transaction is not in the receipts trie of the
    block, or when a trie node could not be fetched. A caller can try
    again later.

    Args:
        node: Node instance to query.
        block_hash: Hash of the block that holds the transaction.
        tx_hash: Hash of the transaction.

    Returns:
        Optional['Receipt']: The receipt, or ``None`` as described above.

    Raises:
        ValueError: If the block cannot be loaded, if the receipt is
            malformed, or if a receipt cell cannot be fetched (raised by
            ``Receipt.from_storage``). A fetch failure is temporary, as
            in the ``None`` case: a caller can try again later.
    """
    from astreum.consensus.block.encoding.decode import get_block_from_storage
    from astreum.consensus.block.holders import block_holder
    from astreum.consensus.models.receipt import Receipt
    from astreum.expression import ZERO32
    from astreum.storage.radix import RadixTree, get_from_radix_tree

    block = get_block_from_storage(
        node, block_hash, contact=block_holder(node, block_hash)
    )
    if block.receipts_hash is None or block.receipts_hash == ZERO32:
        return None

    tree = RadixTree(
        root_hash=block.receipts_hash, contact=block.contact, root_block=block.expr_id
    )
    expr = get_from_radix_tree(tree, node, tx_hash)
    if expr is None:
        return None

    return Receipt.from_storage(
        node, expr.hash(), contact=block.contact, block_hash=block.expr_id
    )
