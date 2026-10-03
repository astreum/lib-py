from __future__ import annotations

from typing import Any

from astreum.expression import resolve_inner_exprs
from astreum.expression import ZERO32
from astreum.consensus.transaction.channel.model import Channel
from astreum.storage.radix import get_from_radix_tree, put_in_radix_tree
from astreum.consensus.transaction.channel.update import get_channel_from_storage


def handle_channel_close(
    *,
    node: Any,
    block: Any,
    sender_account: Any,
    counterparty: bytes,
) -> bool:
    previous_block = getattr(block, "previous_block", None)
    previous_block_time = getattr(previous_block, "timestamp", None)
    if previous_block_time is None:
        return False

    channel_head = get_from_radix_tree(sender_account.channels, node, counterparty)
    channel_state = get_channel_from_storage(node, channel_head, block.contact)
    if channel_state is None:
        return False
    channel_balance, channel_counter, withdrawal_window = channel_state

    if withdrawal_window >= int(previous_block_time):
        return False

    sender_account.balance += channel_balance

    channel = Channel(
        balance=0,
        counter=channel_counter + 1,
        withdrawal_window=withdrawal_window,
    )
    channel_expr = channel.expr()
    updated_channel_head = channel_expr.hash()
    if not updated_channel_head or updated_channel_head == ZERO32:
        return False

    put_in_radix_tree(sender_account.channels, node, counterparty, updated_channel_head)
    sender_account.channels_hash = sender_account.channels.root_hash or ZERO32
    inner_exprs, _ = resolve_inner_exprs(node, channel_expr, contact=block.contact)
    block.pending_exprs.extend(inner_exprs)
    return True
