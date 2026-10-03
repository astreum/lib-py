from __future__ import annotations

from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from astreum.expression import resolve_inner_exprs
from astreum.expression import ZERO32
from astreum.storage.radix import get_from_radix_tree, put_in_radix_tree
from astreum.consensus.transaction.channel.model import Channel
from astreum.consensus.transaction.channel.update import get_channel_from_storage

COUNTER_SIZE = 8
AMOUNT_SIZE = 8


def _withdraw_message(
    *,
    chain_id: int,
    payer: bytes,
    recipient: bytes,
    counter: int,
    amount: int,
) -> bytes:
    return (
        bytes([2])
        + chain_id.to_bytes(8, "little", signed=False)
        + payer
        + recipient
        + counter.to_bytes(COUNTER_SIZE, "little", signed=False)
        + amount.to_bytes(AMOUNT_SIZE, "little", signed=False)
    )


def _data_nodes(data) -> list:
    result = []
    current = data
    while current is not None and getattr(current, "_tag", None) == "link":
        if current._head is not None:
            result.append(current._head)
        current = current._tail
    return result


def handle_channel_withdraw(
    *,
    node: Any,
    block: Any,
    sender_account: Any,
    transaction: Any,
) -> bool:
    nodes = _data_nodes(transaction.data)
    if len(nodes) != 3:
        return False
    counter_node, amount_node, sig_node = nodes
    if counter_node._tag != "int" or amount_node._tag != "int" or sig_node._tag != "bytes":
        return False
    requested_counter = counter_node.value
    requested_amount = amount_node.value
    signature = sig_node.value

    chain_id = transaction.chain_id
    recipient = transaction.sender
    expected_payer = transaction.recipient
    payer = expected_payer

    if requested_amount < 0:
        return False

    try:
        payer_public_key = Ed25519PublicKey.from_public_bytes(payer)
        payer_public_key.verify(
            signature,
            _withdraw_message(
                chain_id=chain_id,
                payer=payer,
                recipient=recipient,
                counter=requested_counter,
                amount=requested_amount,
            ),
        )
    except Exception:
        return False

    payer_account = block.accounts.get_account(address=payer, node=node)
    if payer_account is None:
        return False

    channel_head = get_from_radix_tree(payer_account.channels, node, recipient)
    channel_state = get_channel_from_storage(node, channel_head, block.contact)
    if channel_state is None:
        return False
    channel_balance, stored_counter, withdrawal_window = channel_state

    if block.previous_block.timestamp is None:
        return False

    if block.previous_block.timestamp >= withdrawal_window:
        return False
    if requested_counter <= stored_counter:
        return False
    if requested_amount > channel_balance:
        return False

    updated_balance = channel_balance - requested_amount
    channel = Channel(
        balance=updated_balance,
        counter=requested_counter,
        withdrawal_window=withdrawal_window,
    )
    channel_expr = channel.expr()
    updated_channel_head = channel_expr.hash()
    if not updated_channel_head or updated_channel_head == ZERO32:
        return False

    put_in_radix_tree(payer_account.channels, node, recipient, updated_channel_head)
    payer_account.channels_hash = payer_account.channels.root_hash or ZERO32
    sender_account.balance += requested_amount

    inner_exprs, _ = resolve_inner_exprs(node, channel_expr, contact=block.contact)
    block.pending_exprs.extend(inner_exprs)
    block.accounts.set_account(payer, payer_account)
    return True
