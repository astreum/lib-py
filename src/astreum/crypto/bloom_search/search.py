from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from astreum.expression import Expr, RESOLUTION_FULL, ZERO32
from astreum.storage.exprs import get_expr_list, prefetch_exprs_from_network
from astreum.crypto.bloom_filter import bloom_test
from astreum.crypto.bloom_tree.tree import bloom_search_leaves
from astreum.expression.helpers import get_expr_builtin_tag

if TYPE_CHECKING:
    from astreum.consensus.transaction.model import Transaction

ERA_SIZE = 1024


def bloom_search_tx(astreum_node: Any, *,
                    tx_hash: bytes = ZERO32,
                    sender: bytes = ZERO32,
                    receiver: bytes = ZERO32,
                    key: bytes = ZERO32,
                    involved: bytes = ZERO32,
                    starting_block=None,
                    end_block_height: int = 0,
                    limit: int = 1) -> list["Transaction"]:
    """
    Search for transactions matching filter args.

    Args:
        astreum_node: Astreum node.
        tx_hash: Transaction hash filter.
        sender: Sender address filter.
        receiver: Receiver address filter.
        key: Data key filter.
        involved (bytes): Address of an account that a transaction changed
            without being its sender or recipient. Cannot combine with other
            filters.
        starting_block: Block to start search from.
        end_block_height: Minimum block height.
        limit: Maximum results.

    Returns:
        list["Transaction"]: List of matching transactions.

    Raises:
        ValueError: If involved is set together with another filter.
    """
    from astreum.consensus.transaction.from_storage import get_transaction_from_storage
    from astreum.consensus.block.encoding.decode import get_block_from_storage

    if involved != ZERO32 and any(
        f != ZERO32 for f in (tx_hash, sender, receiver, key)
    ):
        raise ValueError("involved cannot combine with tx_hash, sender, receiver or key")

    if starting_block is None:
        return []

    element = _build_element(tx_hash=tx_hash, sender=sender,
                             receiver=receiver, key=key, involved=involved)

    results: list["Transaction"] = []
    searched_eras: set[int] = set()
    current_block = starting_block

    while current_block is not None and current_block.height >= end_block_height:
        era = current_block.height // ERA_SIZE

        if era not in searched_eras:
            searched_eras.add(era)

            bloom_hash = current_block.bloom_hash
            if not bloom_hash or bloom_hash == ZERO32:
                current_block = current_block.previous_block
                continue

            era_results = bloom_search_leaves(
                bloom_hash, element, astreum_node, block_hash=current_block.expr_id
            )
            for leaf in era_results:
                leaf_hit = leaf.start_hash
                if leaf_hit is not None:
                    try:
                        block = get_block_from_storage(astreum_node=astreum_node, block_hash=leaf_hit)
                    except Exception:
                        continue
                    txs = _load_block_txs(astreum_node, block)
                    block_hash = block.expr_id
                else:
                    # start_hash = None → txs are in the block that owns this bloom tree
                    txs = _load_block_txs(astreum_node, current_block)
                    block_hash = current_block.expr_id

                for tx in txs:
                    tx.block_hash = block_hash
                    if involved != ZERO32:
                        # A transaction does not list the accounts it changed.
                        # The leaf filter holds one element per involved tx.
                        tx_element = _build_element(tx_hash=tx.hash, involved=involved)
                        if not bloom_test(leaf.filter, tx_element):
                            continue
                        results.append(tx)
                        if len(results) >= limit:
                            return results
                        continue
                    if _tx_matches(tx, tx_hash=tx_hash, sender=sender,
                                   receiver=receiver, key=key):
                        results.append(tx)
                        if len(results) >= limit:
                            return results

        current_block = current_block.previous_block

    return results


def _load_block_txs(node: Any, block) -> list["Transaction"]:
    """Load all Transaction objects from a block's transactions_hash.

    Args:
        node: Astreum node.
        block: Block to load from.

    Returns:
        list['Transaction']: Description of the return value.
    """
    from astreum.consensus.transaction.from_storage import get_transaction_from_storage

    if not block.transactions_hash or block.transactions_hash == ZERO32:
        return []

    # The tx trie is all new in its block: its provider holds every tx.
    block_id = getattr(block, "expr_id", None)
    scope = {} if not block_id else {"block_hash": block_id}
    expr = get_expr_list(node, block.transactions_hash, **scope)
    if expr is None:
        return []

    tx_hashes: list[bytes] = []
    current = expr
    while current is not None and get_expr_builtin_tag(current) == "link":
        if current.head_hash is None:
            break
        tx_hashes.append(current.head_hash)
        current = current.tail

    # Fetch the missing tx trees in one batch, not one request per transaction.
    prefetch_exprs_from_network(node, tx_hashes, RESOLUTION_FULL, **scope)

    txs: list["Transaction"] = []
    for tx_hash in tx_hashes:
        try:
            tx = get_transaction_from_storage(node, tx_hash, **scope)
            txs.append(tx)
        except Exception:
            continue

    return txs


def _tx_matches(tx: "Transaction", *,
                tx_hash: bytes = ZERO32,
                sender: bytes = ZERO32,
                receiver: bytes = ZERO32,
                key: bytes = ZERO32) -> bool:
    """
    Check if a Transaction matches the given search params.

    Args:
        tx: Transaction to check.
        tx_hash: Transaction hash filter.
        sender: Sender address filter.
        receiver: Receiver address filter.
        key: Data key filter.

    Returns:
        bool: True if the transaction matches the filters, False otherwise.
    """
    if tx_hash != ZERO32 and tx.hash != tx_hash:
        return False
    if sender != ZERO32 and tx.sender != sender:
        return False
    if receiver != ZERO32 and tx.recipient != receiver:
        return False
    tx_data_bytes = tx.data.value if get_expr_builtin_tag(tx.data) == "bytes" else b""
    if key != ZERO32 and tx_data_bytes != key:
        return False
    return True


def _build_element(*, tx_hash=ZERO32, sender=ZERO32,
                   receiver=ZERO32, key=ZERO32, involved=ZERO32) -> bytes:
    return (tx_hash or ZERO32).ljust(32, b"\x00")[:32] + \
           (sender or ZERO32).ljust(32, b"\x00")[:32] + \
           (receiver or ZERO32).ljust(32, b"\x00")[:32] + \
           (key or ZERO32).ljust(32, b"\x00")[:32] + \
           (involved or ZERO32).ljust(32, b"\x00")[:32]
