from __future__ import annotations

from typing import Any, Optional

from astreum.expression import resolve_inner_exprs
from astreum.expression import ZERO32
from astreum.storage.radix import get_from_radix_tree, put_in_radix_tree
from astreum.consensus.transaction.channel.model import Channel


def get_channel_from_storage(
    node: Any, channel_head: Optional[bytes], contact=None
) -> Optional[tuple[int, int, int]]:
    channel = Channel.from_storage(node, channel_head, contact=contact)
    if channel is None:
        return None
    return channel.balance, channel.counter, channel.withdrawal_window


def handle_channel_update(
    *,
    node: Any,
    block: Any,
    sender_account: Any,
    counterparty: bytes,
    new_withdrawal_window: Optional[int] = None,
    tx_amount: int,
) -> bool:
    if tx_amount < 0:
        return False

    current_channel_head = get_from_radix_tree(sender_account.channels, node, counterparty)
    decoded_record = get_channel_from_storage(node, current_channel_head, block.contact)
    if decoded_record is None:
        return False
    current_balance, current_counter, current_withdrawal_window = decoded_record
    withdrawal_window = (
        current_withdrawal_window
        if new_withdrawal_window is None
        else new_withdrawal_window
    )

    if withdrawal_window < current_withdrawal_window:
        return False

    updated_balance = current_balance
    if tx_amount > 0:
        updated_balance += tx_amount
    updated_counter = current_counter + 1

    channel = Channel(
        balance=updated_balance,
        counter=updated_counter,
        withdrawal_window=withdrawal_window,
    )
    channel_expr = channel.expr()
    channel_head = channel_expr.hash()
    if not channel_head or channel_head == ZERO32:
        return False

    put_in_radix_tree(sender_account.channels, node, counterparty, channel_head)
    sender_account.channels_hash = sender_account.channels.root_hash or ZERO32
    inner_exprs, _ = resolve_inner_exprs(node, channel_expr, contact=block.contact)
    block.pending_exprs.extend(inner_exprs)
    return True
