from __future__ import annotations

from typing import Any

from astreum.expression import resolve_inner_exprs
from astreum.expression import ZERO32
from astreum.storage.radix import RadixTree, get_from_radix_tree, put_in_radix_tree
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.treasury.record import (
    TreasuryCreditOffer,
    TreasuryUserRecord,
)
from astreum.consensus.transaction.treasury.utils import _trie_exprs


def _extend_pending_exprs(block: object, exprs: list) -> None:
    """Append *exprs* to *block*'s pending expr list so they get persisted.

    Args:
        block: The block currently being applied against.
        exprs: Exprs to append to ``block.pending_exprs``.

    Returns:
        None.
    """
    block.pending_exprs.extend(exprs)


def _data_nodes(data) -> list:
    """Flatten a transaction's ``data`` link-list into a plain list of nodes.

    Args:
        data: The head of a ``link`` chain (typically ``transaction.data``).

    Returns:
        The chain's elements in order, as a list of ``Expr`` heads.
    """
    result = []
    current = data
    while current is not None and getattr(current, "_tag", None) == "link":
        if current._head is not None:
            result.append(current._head)
        current = current._tail
    return result


def _current_height(block: object) -> int:
    """Resolve the height of *block*, falling back to ``previous_block`` + 1.

    Args:
        block: The block currently being applied against.

    Returns:
        The block's height as an int.
    """
    return int(
        getattr(
            block,
            "height",
            int(getattr(block.previous_block, "height", -1)) + 1,
        )
    )


def handle_treasury_sell(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
    transaction_hash: bytes,
    sender_account: Any,
    treasury_account: Any,
) -> int:
    """Post a new `TreasuryCreditOffer` into the sender's own offers trie.

    Validates the offer's fields (``limit``, ``duration``, ``price``,
    ``expiry``), loads (or creates, on first post) the sender's
    `TreasuryUserRecord`, inserts the offer into that record's
    ``offers_root_hash`` trie keyed by *transaction_hash*, and writes the
    updated record back. No principal moves on a post — the offer only
    becomes economically active once a consumer claims it (see
    `claim_offer`).

    Args:
        node: Storage node used to resolve and persist trie/expr data.
        block: The block currently being applied against; the offer's
            validity is checked against this block's height and its pending
            exprs are extended with everything the offer/record needs
            persisted.
        transaction: The decoded `TREASURY_SELL` transaction. Its ``data``
            must decode to four ints: ``limit``, ``duration``, ``price``,
            ``expiry`` (in that order).
        transaction_hash: The 32-byte hash of *transaction*; used as the
            offer's key inside the seller's offers trie.
        sender_account: The sender's account (unused directly here — no
            balance changes happen on a post — kept for handler-signature
            consistency with the other treasury handlers).
        treasury_account: The `TREASURY_ADDRESS` account. Its ``data`` trie
            holds every seller's `TreasuryUserRecord`, keyed by sender
            address.

    Returns:
        `STATUS_SUCCESS` on success, `STATUS_FAILED` if any guard fails
        (wrong recipient/sender, malformed data, non-positive `limit`,
        non-pow2 `duration`, negative `price`, already-expired `expiry`, or
        a colliding transaction hash).
    """
    if (
        transaction.recipient != TREASURY_ADDRESS
        or transaction.sender == TREASURY_ADDRESS
        or treasury_account is None
    ):
        return STATUS_FAILED

    nodes = _data_nodes(transaction.data)
    if len(nodes) != 4:
        return STATUS_FAILED
    limit_node, duration_node, price_node, expiry_node = nodes
    if (
        limit_node._tag != "int"
        or duration_node._tag != "int"
        or price_node._tag != "int"
        or expiry_node._tag != "int"
    ):
        return STATUS_FAILED

    limit = limit_node.value
    duration = duration_node.value
    price = price_node.value
    expiry = expiry_node.value

    current_height = _current_height(block)
    if limit <= 0:
        return STATUS_FAILED
    if duration <= 0 or (duration & (duration - 1)) != 0:
        return STATUS_FAILED
    if price < 0:
        return STATUS_FAILED
    if expiry <= current_height:
        return STATUS_FAILED

    user_record_head = get_from_radix_tree(treasury_account.data, node, transaction.sender)
    user_record = TreasuryUserRecord.from_storage(node, user_record_head or ZERO32)
    if user_record is None:
        user_record = TreasuryUserRecord()

    offer_record = TreasuryCreditOffer(
        limit=limit,
        duration=duration,
        price=price,
        expiry=expiry,
        claimed_by=ZERO32,
    )
    offer_record_head = offer_record.expr().hash()

    offers_root_hash = user_record.offers_root_hash or ZERO32
    offers_trie = RadixTree(
        root_hash=None if offers_root_hash == ZERO32 else offers_root_hash
    )
    if get_from_radix_tree(offers_trie, node, transaction_hash) is not None:
        return STATUS_FAILED

    put_in_radix_tree(offers_trie, node, transaction_hash, offer_record_head)
    offer_exprs, _ = resolve_inner_exprs(node, offer_record.expr())

    updated_user_record = TreasuryUserRecord(
        balance=user_record.balance,
        loans_root_hash=user_record.loans_root_hash,
        total_interest_paid=user_record.total_interest_paid,
        offers_root_hash=offers_trie.root_hash or ZERO32,
    )
    updated_user_record_head = updated_user_record.expr().hash()
    put_in_radix_tree(
        treasury_account.data,
        node,
        transaction.sender,
        updated_user_record_head,
    )
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    user_record_exprs, _ = resolve_inner_exprs(node, updated_user_record.expr())
    _extend_pending_exprs(
        block,
        offer_exprs + _trie_exprs(offers_trie) + user_record_exprs,
    )
    return STATUS_SUCCESS


def claim_offer(
    *,
    offers_trie: RadixTree,
    node: Any,
    offer_transaction_id: bytes,
    claimant_id: bytes,
    current_height: int,
) -> TreasuryCreditOffer | None:
    """Claim an available offer in *offers_trie* by its posting transaction id.

    A claim is permanent: once ``claimed_by`` is set it never resets, the
    offer stays claimed in the seller's record forever. There is no "free"
    operation and no transaction code to release a claim.

    Does not touch the seller's ``TreasuryUserRecord.offers_root_hash``
    field itself — the caller must write ``offers_trie.root_hash`` back
    into that field after the call, since the caller is the one holding the
    seller's record in the current transaction's working set (possibly
    alongside several other sellers' records in one transaction, e.g. a
    multi-offer claim).

    Args:
        offers_trie: The seller's offers trie, opened from
            ``TreasuryUserRecord.offers_root_hash``.
        node: Storage node used to resolve and persist trie/expr data.
        offer_transaction_id: The 32-byte `TREASURY_SELL` transaction hash
            identifying the offer to claim (the key it was stored under).
        claimant_id: Opaque 32-byte id to record as the claimant (e.g. a
            loan transaction hash). The offer mechanism itself is agnostic
            to what this id represents.
        current_height: The current block height, used to reject claims on
            an offer whose ``expiry`` has already passed.

    Returns:
        The updated (claimed) `TreasuryCreditOffer` on success, or ``None``
        if the offer doesn't exist, is already claimed
        (``offer.claimed_by != ZERO32``), or is expired
        (``current_height >= offer.expiry``).
    """
    offer_head = get_from_radix_tree(offers_trie, node, offer_transaction_id)
    offer = TreasuryCreditOffer.from_storage(node, offer_head or ZERO32)
    if offer is None:
        return None
    if offer.claimed_by != ZERO32:
        return None
    if current_height >= offer.expiry:
        return None

    updated_offer = TreasuryCreditOffer(
        limit=offer.limit,
        duration=offer.duration,
        price=offer.price,
        expiry=offer.expiry,
        claimed_by=claimant_id,
    )
    updated_offer_head = updated_offer.expr().hash()
    put_in_radix_tree(offers_trie, node, offer_transaction_id, updated_offer_head)
    return updated_offer
