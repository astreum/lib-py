from __future__ import annotations

from dataclasses import replace
from typing import Any

from astreum.expression import Expr, NIL, resolve_inner_exprs, resolve_list_exprs, get_expr_tag
from astreum.expression import ZERO32
from astreum.storage.radix import RadixTree, get_from_radix_tree, get_all_from_radix_tree, put_in_radix_tree
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.account import create_account
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.block.rate_window import windowed_rate_fraction
from astreum.consensus.transaction.treasury.discount import calculate_discounted_amount
from astreum.consensus.transaction.treasury.offers import claim_offer
from astreum.consensus.transaction.treasury.record import (
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
    TreasuryBorrowRequest,
)
from astreum.consensus.transaction.treasury.utils import _remaining_payment_count, _trie_exprs


def _extend_pending_exprs(block: object, exprs: list[Expr]) -> None:
    block.pending_exprs.extend(exprs)


def _data_nodes(data) -> list:
    result = []
    current = data
    while current is not None and getattr(current, "_tag", None) == "link":
        if current._head is not None:
            result.append(current._head)
        current = current._tail
    return result


def _current_height(block: object) -> int:
    return int(
        getattr(
            block,
            "height",
            int(getattr(block.previous_block, "height", -1)) + 1,
        )
    )


def _decode_offer_refs(node: Any, offer_refs_node: Expr) -> list[tuple[bytes, bytes]] | None:
    """Decode a borrow transaction's offer-refs field.

    Args:
        node: Storage node used to resolve any hash-only sub-exprs.
        offer_refs_node: The transaction data's 4th field, as encoded by
            `transaction.create._offer_refs_to_expr` — `NIL` (empty) or a
            `link`-list of `[seller_address, offer_transaction_id]` pairs.

    Returns:
        The decoded `(seller_address, offer_transaction_id)` pairs in
        order, or `None` if the shape doesn't match.
    """
    if offer_refs_node is NIL:
        return []
    entry_nodes, missed = resolve_list_exprs(node, offer_refs_node)
    if missed:
        return None
    result: list[tuple[bytes, bytes]] = []
    for entry_node in entry_nodes:
        sub_nodes, sub_missed = resolve_list_exprs(node, entry_node)
        if sub_missed or len(sub_nodes) != 2:
            return None
        seller_node, offer_node = sub_nodes
        if get_expr_tag(seller_node, node) != "link" or seller_node._head_hash is None:
            return None
        if get_expr_tag(offer_node, node) != "link" or offer_node._head_hash is None:
            return None
        result.append((seller_node._head_hash, offer_node._head_hash))
    return result


def secured_loan_remaining_total(
    *,
    node: Any,
    loans_root_hash: bytes,
) -> int | None:
    if not loans_root_hash or loans_root_hash == ZERO32:
        return 0

    loans_trie = RadixTree(root_hash=loans_root_hash)
    remaining_total = 0
    for loan_record_head in get_all_from_radix_tree(loans_trie, node).values():
        loan = TreasuryLoanRecord.from_storage(node, loan_record_head)
        if loan is None:
            return None
        if loan.loan_type != LoanType.SECURED or loan.next_payment_block_number == 0:
            continue
        remaining_payment_count = _remaining_payment_count(loan)
        if remaining_payment_count is None:
            return None
        remaining_total += remaining_payment_count * loan.payment_amount
    return remaining_total


def _handle_secured_borrow(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
    transaction_hash: bytes,
    sender_account: Any,
    treasury_account: Any,
    request: TreasuryBorrowRequest,
) -> int:
    duration = request.payment_interval_blocks * request.payment_count
    rate_fraction = windowed_rate_fraction(block, duration)
    if rate_fraction is None:
        return STATUS_FAILED

    rate_numerator, rate_denominator = rate_fraction
    discounted_amount = calculate_discounted_amount(
        payment_amount=transaction.amount,
        payment_interval_blocks=request.payment_interval_blocks,
        payment_count=request.payment_count,
        rate_numerator=rate_numerator,
        rate_denominator=rate_denominator,
    )
    scheduled_total = transaction.amount * request.payment_count
    user_record_head = get_from_radix_tree(treasury_account.data, node, transaction.sender)
    user_record = TreasuryUserRecord.from_storage(node, user_record_head or ZERO32)
    existing_secured_total = (
        None
        if user_record is None
        else secured_loan_remaining_total(
            node=node,
            loans_root_hash=user_record.loans_root_hash or ZERO32,
        )
    )
    if (
        discounted_amount is None
        or discounted_amount <= 0
        or user_record is None
        or existing_secured_total is None
        or user_record.balance - existing_secured_total < scheduled_total
        or treasury_account.balance < discounted_amount
    ):
        return STATUS_FAILED

    creation_block_number = _current_height(block)
    next_payment_block_number = creation_block_number + request.payment_interval_blocks
    loan_record = TreasuryLoanRecord(
        creation_block_number=creation_block_number,
        loan_type=request.loan_type,
        discounted_amount=discounted_amount,
        payment_amount=transaction.amount,
        payment_interval_blocks=request.payment_interval_blocks,
        next_payment_block_number=next_payment_block_number,
        payment_count=request.payment_count,
    )
    loan_record_head = loan_record.expr().hash()
    loans_root_hash = user_record.loans_root_hash or ZERO32
    loans_trie = RadixTree(
        root_hash=None if loans_root_hash == ZERO32 else loans_root_hash
    )
    if get_from_radix_tree(loans_trie, node, transaction_hash) is not None:
        return STATUS_FAILED

    put_in_radix_tree(loans_trie, node, transaction_hash, loan_record_head)
    loan_exprs, _ = resolve_inner_exprs(node, loan_record.expr())
    user_record = replace(user_record, loans_root_hash=loans_trie.root_hash or ZERO32)
    updated_user_record_head = user_record.expr().hash()
    put_in_radix_tree(
        treasury_account.data,
        node,
        transaction.sender,
        updated_user_record_head,
    )
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    treasury_account.balance -= discounted_amount
    sender_account.balance += discounted_amount
    user_record_exprs, _ = resolve_inner_exprs(node, user_record.expr())
    _extend_pending_exprs(
        block,
        loan_exprs + _trie_exprs(loans_trie) + user_record_exprs,
    )
    return STATUS_SUCCESS


def _handle_unsecured_borrow(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
    transaction_hash: bytes,
    sender_account: Any,
    treasury_account: Any,
    request: TreasuryBorrowRequest,
    offer_refs: list[tuple[bytes, bytes]],
) -> int:
    if not offer_refs:
        return STATUS_FAILED
    if len(set(offer_refs)) != len(offer_refs):
        return STATUS_FAILED

    duration = request.payment_interval_blocks * request.payment_count
    rate_fraction = windowed_rate_fraction(block, duration)
    if rate_fraction is None:
        return STATUS_FAILED

    rate_numerator, rate_denominator = rate_fraction
    discounted_amount = calculate_discounted_amount(
        payment_amount=transaction.amount,
        payment_interval_blocks=request.payment_interval_blocks,
        payment_count=request.payment_count,
        rate_numerator=rate_numerator,
        rate_denominator=rate_denominator,
    )
    if discounted_amount is None or discounted_amount <= 0:
        return STATUS_FAILED

    creation_block_number = _current_height(block)

    borrower_record_head = get_from_radix_tree(treasury_account.data, node, transaction.sender)
    borrower_record = TreasuryUserRecord.from_storage(node, borrower_record_head or ZERO32)
    if borrower_record is None:
        borrower_record = TreasuryUserRecord()

    # Claim every referenced offer, grouping per-seller state so a borrow
    # that claims several offers from the same seller only loads/writes
    # that seller's record once.
    seller_states: dict[bytes, dict] = {}
    claimed_entries: list[tuple[bytes, bytes, int]] = []
    sum_limit = 0

    for seller_address, offer_transaction_id in offer_refs:
        if seller_address == TREASURY_ADDRESS:
            return STATUS_FAILED

        state = seller_states.get(seller_address)
        if state is None:
            seller_record_head = get_from_radix_tree(treasury_account.data, node, seller_address)
            seller_record = TreasuryUserRecord.from_storage(node, seller_record_head or ZERO32)
            if seller_record is None:
                return STATUS_FAILED
            offers_root_hash = seller_record.offers_root_hash or ZERO32
            offers_trie = RadixTree(
                root_hash=None if offers_root_hash == ZERO32 else offers_root_hash
            )
            state = {
                "record": seller_record,
                "trie": offers_trie,
                "price_total": 0,
                "added_limit": 0,
            }
            seller_states[seller_address] = state

        claimed_offer = claim_offer(
            offers_trie=state["trie"],
            node=node,
            offer_transaction_id=offer_transaction_id,
            claimant_id=transaction_hash,
            current_height=creation_block_number,
        )
        if claimed_offer is None:
            return STATUS_FAILED
        if claimed_offer.duration != duration:
            return STATUS_FAILED

        state["price_total"] += claimed_offer.price
        state["added_limit"] += claimed_offer.limit
        sum_limit += claimed_offer.limit
        claimed_entries.append((seller_address, offer_transaction_id, claimed_offer.limit))

    if sum_limit < discounted_amount:
        return STATUS_FAILED

    for state in seller_states.values():
        record = state["record"]
        if record.sold_limit + state["added_limit"] > record.total_interest_paid:
            return STATUS_FAILED

    gc = getattr(block, "global_loan_count", 0) or 0
    gd = getattr(block, "global_defaulted", 0) or 0
    gl = getattr(block, "global_loaned", 0) or 0
    if gc <= 0:
        insurance_fee = 0
    else:
        denominator = borrower_record.loaned * gc + gl
        if denominator <= 0:
            return STATUS_FAILED
        numerator = discounted_amount * (borrower_record.defaulted * gc + gd)
        insurance_fee = numerator // denominator

    total_price = sum(state["price_total"] for state in seller_states.values())
    net_amount = discounted_amount - insurance_fee - total_price
    if net_amount <= 0:
        return STATUS_FAILED
    if treasury_account.balance < discounted_amount - insurance_fee:
        return STATUS_FAILED

    next_payment_block_number = creation_block_number + request.payment_interval_blocks
    loan_record = TreasuryLoanRecord(
        creation_block_number=creation_block_number,
        loan_type=LoanType.UNSECURED,
        discounted_amount=discounted_amount,
        payment_amount=transaction.amount,
        payment_interval_blocks=request.payment_interval_blocks,
        next_payment_block_number=next_payment_block_number,
        payment_count=request.payment_count,
        claimed_offers=claimed_entries,
        insurance_fee=insurance_fee,
        missed_count=0,
    )
    loan_record_head = loan_record.expr().hash()

    loans_root_hash = borrower_record.loans_root_hash or ZERO32
    loans_trie = RadixTree(
        root_hash=None if loans_root_hash == ZERO32 else loans_root_hash
    )
    if get_from_radix_tree(loans_trie, node, transaction_hash) is not None:
        return STATUS_FAILED
    put_in_radix_tree(loans_trie, node, transaction_hash, loan_record_head)
    loan_exprs, _ = resolve_inner_exprs(node, loan_record.expr())

    pending_exprs: list[Expr] = list(loan_exprs) + _trie_exprs(loans_trie)

    # Pay each seller its offer(s)' price and write back its updated
    # offers_root_hash + sold_limit.
    for seller_address, state in seller_states.items():
        seller_account = block.accounts.get_account(seller_address, node)
        if seller_account is None:
            seller_account = create_account()
        seller_account.balance += state["price_total"]
        block.accounts.set_account(seller_address, seller_account)

        updated_seller_record = replace(
            state["record"],
            offers_root_hash=state["trie"].root_hash or ZERO32,
            sold_limit=state["record"].sold_limit + state["added_limit"],
        )
        updated_seller_record_head = updated_seller_record.expr().hash()
        put_in_radix_tree(treasury_account.data, node, seller_address, updated_seller_record_head)
        seller_record_exprs, _ = resolve_inner_exprs(node, updated_seller_record.expr())
        pending_exprs.extend(seller_record_exprs + _trie_exprs(state["trie"]))

    updated_borrower_record = replace(
        borrower_record,
        loans_root_hash=loans_trie.root_hash or ZERO32,
        loaned=borrower_record.loaned + discounted_amount,
    )
    updated_borrower_record_head = updated_borrower_record.expr().hash()
    put_in_radix_tree(treasury_account.data, node, transaction.sender, updated_borrower_record_head)
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    borrower_record_exprs, _ = resolve_inner_exprs(node, updated_borrower_record.expr())
    pending_exprs.extend(borrower_record_exprs)

    treasury_account.balance -= discounted_amount - insurance_fee
    sender_account.balance += net_amount

    block.global_loaned = getattr(block, "global_loaned", 0) + discounted_amount
    block.global_loan_count = getattr(block, "global_loan_count", 0) + 1

    _extend_pending_exprs(block, pending_exprs)
    return STATUS_SUCCESS


def handle_treasury_borrow(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
    transaction_hash: bytes,
    sender_account: Any,
    treasury_account: Any,
) -> int:
    if (
        transaction.recipient != TREASURY_ADDRESS
        or transaction.sender == TREASURY_ADDRESS
        or transaction.amount <= 0
        or treasury_account is None
    ):
        return STATUS_FAILED

    nodes = _data_nodes(transaction.data)
    if len(nodes) != 4:
        return STATUS_FAILED
    loan_type_node, interval_node, count_node, offer_refs_node = nodes
    if loan_type_node._tag != "int" or interval_node._tag != "int" or count_node._tag != "int":
        return STATUS_FAILED
    request = TreasuryBorrowRequest(
        loan_type=LoanType(loan_type_node.value),
        payment_interval_blocks=interval_node.value,
        payment_count=count_node.value,
    )

    if request.loan_type == LoanType.SECURED:
        return _handle_secured_borrow(
            node=node,
            block=block,
            transaction=transaction,
            transaction_hash=transaction_hash,
            sender_account=sender_account,
            treasury_account=treasury_account,
            request=request,
        )

    if request.loan_type == LoanType.UNSECURED:
        offer_refs = _decode_offer_refs(node, offer_refs_node)
        if offer_refs is None:
            return STATUS_FAILED
        return _handle_unsecured_borrow(
            node=node,
            block=block,
            transaction=transaction,
            transaction_hash=transaction_hash,
            sender_account=sender_account,
            treasury_account=treasury_account,
            request=request,
            offer_refs=offer_refs,
        )

    return STATUS_FAILED
