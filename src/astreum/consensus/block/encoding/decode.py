from __future__ import annotations

from typing import Any

from astreum.expression import Expr, NIL, get_expr_head_hash, resolve_list_exprs, get_expr_tag, get_expr_builtin_tag, get_expr_value
from astreum.storage.exprs import get_expr_list
from astreum.consensus.models.block import Block
from astreum.consensus.block.create import create_block
from astreum.crypto.bloom_tree import BloomTree


def get_block_from_storage(
    astreum_node: Any, block_hash: bytes, contact=None, load_bloom: bool = True
) -> Block:
    """Deserialize a Block from its S-expression stored at the given hash.

    Fetches and resolves the block header expression chain from storage
    and reconstructs a Block from it, including its bloom tree.

    Args:
        astreum_node: A Node instance for fetching expressions from storage.
        block_hash: The content hash of the serialized block.
        contact: Optional ``RelayEndpoint`` of the validator that
            holds the block's uncommitted exprs. Every network fetch for the
            block goes there, and the block keeps it (``block.contact``) so
            its tries and bloom tree do the same.
        load_bloom: Load the bloom tree root (a storage read that can go to
            the network). Pass False to decode the header only.

    Returns:
        The deserialized Block.

    Raises:
        ValueError: If the block header cannot be fetched or parsed.
    """
    header = get_expr_list(astreum_node, block_hash, contact=contact, block_hash=block_hash)
    if header is None:
        raise ValueError("unable to load block header from storage")
    if not get_expr_builtin_tag(header) == "link":
        raise ValueError("block header must be a Link")
    if header.tail is None or get_expr_builtin_tag(header.tail) != "symbol" or header.tail.value != "block":
        raise ValueError(
            f"invalid block type tag (got {header.tail!r})"
        )

    inner = header.head
    if inner is None and header.head_hash is not None:
        from astreum.storage.exprs import get_expr
        inner = get_expr(astreum_node, header.head_hash, contact=contact, block_hash=block_hash)
        if inner is not None:
            header.head = inner
    if inner is None or get_expr_builtin_tag(inner) != "link":
        raise ValueError("block inner header must be a Link")

    inner_nodes, missed = resolve_list_exprs(astreum_node, inner, contact=contact, block_hash=block_hash)
    if missed:
        raise ValueError(
            f"unable to resolve block header (missed={[h.hex()[:16] for h in missed]})"
        )
    if len(inner_nodes) != 2:
        raise ValueError(
            f"malformed block header length (got={len(inner_nodes)}, expected=2)"
        )

    body, sig = inner_nodes
    if not get_expr_builtin_tag(sig) == "bytes":
        raise ValueError("invalid block signature: expected Bytes")
    signature_bytes = sig.value
    if not get_expr_builtin_tag(body) == "link":
        raise ValueError("block body must be a Link chain")

    body_nodes, missed = resolve_list_exprs(astreum_node, body, contact=contact, block_hash=block_hash)
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
        accounts_link,
        bloom_link,
        chain_id_node,
        difficulty_node,
        height_node,
        nonce_node,
        prev_link,
        previous_era_link,
        receipts_link,
        timestamp_node,
        total_storage_fee_node,
        total_transaction_fee_node,
        transactions_link,
        validator_node,
        statistics_node,
    ) = body_nodes

    if not get_expr_tag(global_loaned_node, astreum_node, contact, block_hash) == "int":
        raise ValueError("expected Int for global_loaned")
    if not get_expr_tag(global_defaulted_node, astreum_node, contact, block_hash) == "int":
        raise ValueError("expected Int for global_defaulted")
    if not get_expr_tag(global_loan_count_node, astreum_node, contact, block_hash) == "int":
        raise ValueError("expected Int for global_loan_count")
    if not get_expr_tag(accounts_link, astreum_node, contact, block_hash) == "link":
        raise ValueError("expected Link for accounts_hash")
    if not get_expr_tag(bloom_link, astreum_node, contact, block_hash) == "link":
        raise ValueError("expected Link for bloom_hash")
    if not get_expr_tag(chain_id_node, astreum_node, contact, block_hash) == "int":
        raise ValueError("expected Int for chain_id")
    if not get_expr_tag(difficulty_node, astreum_node, contact, block_hash) == "int":
        raise ValueError("expected Int for difficulty")
    if not get_expr_tag(height_node, astreum_node, contact, block_hash) == "int":
        raise ValueError("expected Int for height")
    if not get_expr_tag(nonce_node, astreum_node, contact, block_hash) == "int":
        raise ValueError("expected Int for nonce")
    if not get_expr_tag(prev_link, astreum_node, contact, block_hash) == "link":
        raise ValueError("expected Link for previous_block_hash")
    if not get_expr_tag(previous_era_link, astreum_node, contact, block_hash) == "link":
        raise ValueError("expected Link for previous_era_hash")
    if not get_expr_tag(receipts_link, astreum_node, contact, block_hash) == "link":
        raise ValueError("expected Link for receipts_hash")
    if not get_expr_tag(timestamp_node, astreum_node, contact, block_hash) == "int":
        raise ValueError("expected Int for timestamp")
    if not get_expr_tag(total_storage_fee_node, astreum_node, contact, block_hash) == "int":
        raise ValueError("expected Int for total_storage_fee")
    if not get_expr_tag(total_transaction_fee_node, astreum_node, contact, block_hash) == "int":
        raise ValueError("expected Int for total_transaction_fee")
    if not get_expr_tag(transactions_link, astreum_node, contact, block_hash) == "link":
        raise ValueError("expected Link for transactions_hash")
    if not get_expr_tag(validator_node, astreum_node, contact, block_hash) == "bytes":
        raise ValueError("expected Bytes for validator_public_key_bytes")

    if get_expr_tag(statistics_node, astreum_node, contact, block_hash) == "link":
        stat_nodes, missed = resolve_list_exprs(astreum_node, statistics_node, contact=contact, block_hash=block_hash)
        if missed:
            raise ValueError(
                f"unable to resolve statistics (missed={[h.hex()[:8] for h in missed]})"
            )
        statistics = []
        for j, entry_node in enumerate(stat_nodes):
            if entry_node is NIL:
                continue
            int_nodes, missed = resolve_list_exprs(astreum_node, entry_node, contact=contact, block_hash=block_hash)
            if missed:
                raise ValueError(
                    f"unable to resolve statistics entry {j} (missed={[h.hex()[:8] for h in missed]})"
                )
            if j == 0 and len(int_nodes) == 2:
                statistics.append((get_expr_value(int_nodes[0], astreum_node, contact, block_hash), get_expr_value(int_nodes[1], astreum_node, contact, block_hash), 0, 0))
            elif len(int_nodes) == 4:
                statistics.append((get_expr_value(int_nodes[0], astreum_node, contact, block_hash), get_expr_value(int_nodes[1], astreum_node, contact, block_hash), get_expr_value(int_nodes[2], astreum_node, contact, block_hash), get_expr_value(int_nodes[3], astreum_node, contact, block_hash)))
            else:
                raise ValueError(
                    f"invalid statistics entry {j} length (got={len(int_nodes)})"
                )
    elif get_expr_tag(statistics_node, astreum_node, contact, block_hash) == "symbol":
        statistics = []
    else:
        raise ValueError("expected Link or Symbol for statistics")

    block = create_block(
        chain_id=get_expr_value(chain_id_node, astreum_node, contact, block_hash),
        previous_block_hash=get_expr_head_hash(prev_link),
        previous_block=None,
        height=get_expr_value(height_node, astreum_node, contact, block_hash),
        timestamp=get_expr_value(timestamp_node, astreum_node, contact, block_hash),
        accounts_hash=get_expr_head_hash(accounts_link),
        total_transaction_fee=get_expr_value(total_transaction_fee_node, astreum_node, contact, block_hash),
        total_storage_fee=get_expr_value(total_storage_fee_node, astreum_node, contact, block_hash),
        transactions_hash=get_expr_head_hash(transactions_link),
        receipts_hash=get_expr_head_hash(receipts_link),
        difficulty=get_expr_value(difficulty_node, astreum_node, contact, block_hash),
        validator_public_key_bytes=get_expr_value(validator_node, astreum_node, contact, block_hash),
        nonce=get_expr_value(nonce_node, astreum_node, contact, block_hash),
        bloom_hash=get_expr_head_hash(bloom_link),
        previous_era_hash=get_expr_head_hash(previous_era_link),
        signature=signature_bytes,
        expr_id=block_hash,
        body_hash=body.hash(),
        statistics=statistics,
        global_loaned=get_expr_value(global_loaned_node, astreum_node, contact, block_hash),
        global_defaulted=get_expr_value(global_defaulted_node, astreum_node, contact, block_hash),
        global_loan_count=get_expr_value(global_loan_count_node, astreum_node, contact, block_hash),
        contact=contact,
    )

    if load_bloom:
        # The bloom nodes are new in this block: its provider holds them.
        block.bloom_tree = BloomTree(
            block.bloom_hash, astreum_node, contact=contact, block_hash=block_hash
        )

    return block
