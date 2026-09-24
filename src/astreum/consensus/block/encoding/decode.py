from __future__ import annotations

from typing import Any

from astreum.expression import Expr, NIL, resolve_list_exprs, get_expr_tag, get_expr_value
from astreum.storage.exprs import get_expr_list
from astreum.consensus.models.block import Block
from astreum.consensus.block.create import create_block
from astreum.crypto.bloom_tree import BloomTree


def get_block_from_storage(astreum_node: Any, block_hash: bytes) -> Block:
    """Deserialize a Block from its S-expression stored at the given hash.

    Fetches and resolves the block header expression chain from storage
    and reconstructs a Block from it, including its bloom tree.

    Args:
        astreum_node: A Node instance for fetching expressions from storage.
        block_hash: The content hash of the serialized block.

    Returns:
        The deserialized Block.

    Raises:
        ValueError: If the block header cannot be fetched or parsed.
    """
    header = get_expr_list(astreum_node, block_hash)
    if header is None:
        raise ValueError("unable to load block header from storage")
    if not header._tag == "link":
        raise ValueError("block header must be a Link")
    if header._tail is None or header._tail._tag != "symbol" or header._tail.value != "block":
        raise ValueError(
            f"invalid block type tag (got {header._tail!r})"
        )

    inner = header._head
    if inner is None and header._head_hash is not None:
        from astreum.storage.exprs import get_expr
        inner = get_expr(astreum_node, header._head_hash)
        if inner is not None:
            header._head = inner
    if inner is None or inner._tag != "link":
        raise ValueError("block inner header must be a Link")

    inner_nodes, missed = resolve_list_exprs(astreum_node, inner)
    if missed:
        raise ValueError(
            f"unable to resolve block header (missed={[h.hex()[:16] for h in missed]})"
        )
    if len(inner_nodes) != 2:
        raise ValueError(
            f"malformed block header length (got={len(inner_nodes)}, expected=2)"
        )

    body, sig = inner_nodes
    if not sig._tag == "bytes":
        raise ValueError("invalid block signature: expected Bytes")
    signature_bytes = sig.value
    if not body._tag == "link":
        raise ValueError("block body must be a Link chain")

    body_nodes, missed = resolve_list_exprs(astreum_node, body)
    if missed:
        raise ValueError(
            f"unable to resolve block body (missed={[h.hex()[:8] for h in missed]})"
        )
    if len(body_nodes) != 18:
        raise ValueError(
            f"malformed block body length (got={len(body_nodes)}, expected=18)"
        )

    (
        global_loaned_node,
        global_defaulted_node,
        global_loan_count_node,
        accounts_node,
        bloom_hash_node,
        chain_id_node,
        difficulty_node,
        height_node,
        nonce_node,
        prev_node,
        previous_era_hash_node,
        receipts_node,
        timestamp_node,
        total_storage_fee_node,
        total_transaction_fee_node,
        transactions_node,
        validator_node,
        statistics_node,
    ) = body_nodes

    if not get_expr_tag(global_loaned_node, astreum_node) == "int":
        raise ValueError("expected Int for global_loaned")
    if not get_expr_tag(global_defaulted_node, astreum_node) == "int":
        raise ValueError("expected Int for global_defaulted")
    if not get_expr_tag(global_loan_count_node, astreum_node) == "int":
        raise ValueError("expected Int for global_loan_count")
    if not get_expr_tag(accounts_node, astreum_node) == "link":
        raise ValueError("expected Link for accounts_hash")
    if not get_expr_tag(bloom_hash_node, astreum_node) == "link":
        raise ValueError("expected Link for bloom_hash")
    if not get_expr_tag(chain_id_node, astreum_node) == "int":
        raise ValueError("expected Int for chain_id")
    if not get_expr_tag(difficulty_node, astreum_node) == "int":
        raise ValueError("expected Int for difficulty")
    if not get_expr_tag(height_node, astreum_node) == "int":
        raise ValueError("expected Int for height")
    if not get_expr_tag(nonce_node, astreum_node) == "int":
        raise ValueError("expected Int for nonce")
    if not get_expr_tag(prev_node, astreum_node) == "link":
        raise ValueError("expected Link for previous_block_hash")
    if not get_expr_tag(previous_era_hash_node, astreum_node) == "link":
        raise ValueError("expected Link for previous_era_hash")
    if not get_expr_tag(receipts_node, astreum_node) == "link":
        raise ValueError("expected Link for receipts_hash")
    if not get_expr_tag(timestamp_node, astreum_node) == "int":
        raise ValueError("expected Int for timestamp")
    if not get_expr_tag(total_storage_fee_node, astreum_node) == "int":
        raise ValueError("expected Int for total_storage_fee")
    if not get_expr_tag(total_transaction_fee_node, astreum_node) == "int":
        raise ValueError("expected Int for total_transaction_fee")
    if not get_expr_tag(transactions_node, astreum_node) == "link":
        raise ValueError("expected Link for transactions_hash")
    if not get_expr_tag(validator_node, astreum_node) == "bytes":
        raise ValueError("expected Bytes for validator_public_key_bytes")

    if get_expr_tag(statistics_node, astreum_node) == "link":
        stat_nodes, missed = resolve_list_exprs(astreum_node, statistics_node)
        if missed:
            raise ValueError(
                f"unable to resolve statistics (missed={[h.hex()[:8] for h in missed]})"
            )
        statistics = []
        for j, entry_node in enumerate(stat_nodes):
            if entry_node is NIL:
                continue
            int_nodes, missed = resolve_list_exprs(astreum_node, entry_node)
            if missed:
                raise ValueError(
                    f"unable to resolve statistics entry {j} (missed={[h.hex()[:8] for h in missed]})"
                )
            if j == 0 and len(int_nodes) == 2:
                statistics.append((get_expr_value(int_nodes[0], astreum_node), get_expr_value(int_nodes[1], astreum_node), 0, 0))
            elif len(int_nodes) == 4:
                statistics.append((get_expr_value(int_nodes[0], astreum_node), get_expr_value(int_nodes[1], astreum_node), get_expr_value(int_nodes[2], astreum_node), get_expr_value(int_nodes[3], astreum_node)))
            else:
                raise ValueError(
                    f"invalid statistics entry {j} length (got={len(int_nodes)})"
                )
    elif get_expr_tag(statistics_node, astreum_node) == "symbol":
        statistics = []
    else:
        raise ValueError("expected Link or Symbol for statistics")

    block = create_block(
        chain_id=get_expr_value(chain_id_node, astreum_node),
        previous_block_hash=prev_node._head_hash,
        previous_block=None,
        height=get_expr_value(height_node, astreum_node),
        timestamp=get_expr_value(timestamp_node, astreum_node),
        accounts_hash=accounts_node._head_hash,
        total_transaction_fee=get_expr_value(total_transaction_fee_node, astreum_node),
        total_storage_fee=get_expr_value(total_storage_fee_node, astreum_node),
        transactions_hash=transactions_node._head_hash,
        receipts_hash=receipts_node._head_hash,
        difficulty=get_expr_value(difficulty_node, astreum_node),
        validator_public_key_bytes=get_expr_value(validator_node, astreum_node),
        nonce=get_expr_value(nonce_node, astreum_node),
        bloom_hash=bloom_hash_node._head_hash,
        previous_era_hash=previous_era_hash_node._head_hash,
        signature=signature_bytes,
        expr_id=block_hash,
        body_hash=body.hash(),
        statistics=statistics,
        global_loaned=get_expr_value(global_loaned_node, astreum_node),
        global_defaulted=get_expr_value(global_defaulted_node, astreum_node),
        global_loan_count=get_expr_value(global_loan_count_node, astreum_node),
    )

    block.bloom_tree = BloomTree(block.bloom_hash, astreum_node)

    return block
