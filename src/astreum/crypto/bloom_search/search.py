from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterator, Optional

from astreum.expression import RESOLUTION_FULL, ZERO32
from astreum.storage.exprs import get_expr_list, prefetch_exprs_from_network
from astreum.crypto.bloom_filter import bloom_test
from astreum.crypto.bloom_search.variants import make_search_variants
from astreum.crypto.bloom_tree.tree import bloom_search_leaves
from astreum.expression.helpers import get_expr_builtin_tag
from astreum.consensus.block.previous import get_previous_block

if TYPE_CHECKING:
    from astreum.consensus.transaction.model import Transaction

ERA_SIZE = 1024


def _matched_keys(tx, leaf, keys):
    """Return the subset of keys that the transaction matches.

    Base keys (fifth column zero) match by exact membership in the
    transaction's own indexed variants. Involved keys (fifth column non-zero)
    are matched with a bloom test, because a transaction does not list the
    accounts it changed.

    Args:
        tx: Transaction to check.
        leaf: Bloom leaf whose filter holds the involved elements.
        keys: Candidate 160-byte keys.

    Returns:
        list[bytes]: The matching keys, in the order of ``keys``.
    """
    z = ZERO32
    base = set(make_search_variants(tx.hash, tx.sender, tx.recipient))
    matched = []
    for k in keys:
        if k[128:160] == z:
            # base key: exact membership in the tx's own 7 indexed variants
            if k in base:
                matched.append(k)
        else:
            # involved key: account is the fifth column. The tx does not list
            # its involved accounts, so this is a bloom test (probabilistic).
            account = k[128:160]
            th = k[0:32] if k[0:32] != z else tx.hash
            if bloom_test(leaf.filter, th + z + z + z + account):
                matched.append(k)
    return matched


def iter_matching_transactions(astreum_node: Any, *,
                               keys: list[bytes],
                               starting_block,
                               end_block_height: int = 0,
                               limit: int = 1) -> Iterator[tuple["Transaction", list[bytes]]]:
    """Yield transactions that match any of ``keys``, newest first.

    Each key has its own result budget. ``limit == 0`` makes every key
    unlimited. A key stops matching once its budget reaches zero.

    Args:
        astreum_node: Astreum node.
        keys: Candidate 160-byte search keys.
        starting_block: Block to start search from.
        end_block_height: Minimum block height.
        limit: Maximum results per key (0 means unlimited).

    Yields:
        tuple["Transaction", list[bytes]]: A matching transaction and the
        subset of ``keys`` it matched.
    """
    from astreum.consensus.block.encoding.decode import get_block_from_storage

    keys = list(dict.fromkeys(keys))
    if not keys or starting_block is None:
        return

    budget: dict[bytes, Optional[int]] = {
        k: (None if limit == 0 else limit) for k in keys
    }

    searched_eras: set[int] = set()
    current_block = starting_block

    while current_block is not None and current_block.height >= end_block_height:
        era = current_block.height // ERA_SIZE

        if era not in searched_eras:
            searched_eras.add(era)

            bloom_hash = current_block.bloom_hash
            if not bloom_hash or bloom_hash == ZERO32:
                current_block = get_previous_block(astreum_node, current_block)
                continue

            leaves: dict[Optional[bytes], Any] = {}
            for key in keys:
                for leaf in bloom_search_leaves(
                    bloom_hash, key, astreum_node,
                    block_hash=current_block.expr_id,
                ):
                    leaves[leaf.start_hash] = leaf

            for leaf in leaves.values():
                leaf_hit = leaf.start_hash
                if leaf_hit is not None:
                    try:
                        block = get_block_from_storage(
                            astreum_node=astreum_node, block_hash=leaf_hit
                        )
                    except Exception:
                        continue
                    txs = _load_block_txs(astreum_node, block)
                    block_hash = block.expr_id
                else:
                    txs = _load_block_txs(astreum_node, current_block)
                    block_hash = current_block.expr_id

                for tx in txs:
                    tx.block_hash = block_hash
                    matched = _matched_keys(tx, leaf, keys)
                    active = [k for k in matched if budget[k] != 0]
                    if not active:
                        continue
                    yield tx, active
                    for k in active:
                        if budget[k] is not None:
                            budget[k] -= 1

                    # Stop as soon as every key's budget is exhausted. A
                    # ``None`` budget (unlimited) never equals 0, so it keeps
                    # the search going, which is correct.
                    if all(b == 0 for b in budget.values()):
                        return

        current_block = get_previous_block(astreum_node, current_block)


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
