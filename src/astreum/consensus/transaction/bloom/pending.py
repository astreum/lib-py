from astreum.consensus.block.rate import calculate_storage_fee
from astreum.consensus.models.receipt import STATUS_SUCCESS


def finalize_pending_bloom_inserts(node, block, transaction, receipt_status) -> int:
    """Insert pending bloom variants if tx succeeded. Returns storage fee.

    Args:
        node: The node.
        block: The block.
        transaction: The transaction.
        receipt_status: The receipt status.

    Returns:
        The storage fee.
    """
    keys = transaction.pending_bloom_keys
    inserts = transaction.pending_bloom_inserts
    if receipt_status != STATUS_SUCCESS or not keys:
        keys.clear()
        inserts.clear()
        return 0

    block_offset = block.height % 1024
    block.bloom_tree.insert(block_offset, list(inserts), node)

    block.pending_bloom_keys |= keys
    fee = calculate_storage_fee(block, 8 * len(keys))
    keys.clear()
    inserts.clear()
    return fee
