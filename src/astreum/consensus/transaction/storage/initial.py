from __future__ import annotations

from typing import Any, List, Optional, Tuple

from astreum.consensus.block.rate import calculate_storage_fee
from astreum.expression import Expr, resolve_inner_exprs, resolve_list_exprs
from astreum.expression import ZERO32
from astreum.storage.exprs import get_expr_list
from astreum.storage.radix import get_from_radix_tree, put_in_radix_tree
from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.storage.model import StorageRecord, StorageSlot

def generate_initial_storage_record(
    node: Any,
    block: object,
    expr: Expr,
    temp_exprs: dict[bytes, Expr] | None = None,
    mint: bool = False,
) -> Tuple[StorageRecord, dict[bytes, StorageSlot], List[bytes], int] | None:
    """
    Walk expr tree, slot new sub-exprs not yet registered in storage account data.
    Returns (StorageRecord, slot_map, found_exprs, storage_fee) for a fresh
    expr, or None if already registered.  slot_map maps each new sub-expr's
    hash to its StorageSlot.  found_exprs contains hashes of sub-exprs already
    in storage data (shared refs).
    """
    storage_account = block.accounts.get_account(STORAGE_ADDRESS, node)
    storage_data = storage_account.data

    root_hash = expr.hash()
    existing = get_from_radix_tree(storage_data, node, root_hash)
    if existing is not None:
        return None  # Already registered — nothing to do

    storage_id = root_hash
    slot_map: dict[bytes, StorageSlot] = {}
    found_exprs: list[bytes] = []
    total_new_size = 0

    def _slot_if_new(sub_expr: Expr) -> None:
        nonlocal total_new_size
        h = sub_expr.hash()
        if h in slot_map or get_from_radix_tree(storage_data, node, h) is not None:
            if h not in slot_map:
                found_exprs.append(h)
            return  # Already slotted or shared reference — skip entire subtree
        slot_map[h] = StorageSlot(
            storage_id=storage_id,
            sequence=len(slot_map),
        )
        total_new_size += sub_expr.size()
        if sub_expr._tag == "link":
            if sub_expr._head is not None:
                _slot_if_new(sub_expr._head)
            if sub_expr._tail is not None:
                _slot_if_new(sub_expr._tail)
            if sub_expr._head_hash is not None and sub_expr._head is None:
                ptr = sub_expr._head_hash
                if get_from_radix_tree(storage_data, node, ptr) is not None:
                    return
                if temp_exprs is not None:
                    target = temp_exprs.get(ptr)
                    if target is not None and target._tag == "link":
                        if target._head is not None:
                            _slot_if_new(target._head)
                        if target._tail is not None:
                            _slot_if_new(target._tail)

    if expr._tag == "link":
        if expr._head is not None:
            _slot_if_new(expr._head)
        if expr._tail is not None:
            _slot_if_new(expr._tail)

    storage_fee = calculate_storage_fee(block, total_new_size)
    record = StorageRecord(
        creation_block_hash=block.previous_block_hash,
        last_payment_block_hash=block.previous_block_hash,
        last_payment_height=block.height - 1,
        last_payment_winner=ZERO32,
        new_size=total_new_size,
        new_count=len(slot_map),
        mint=mint,
    )
    return record, slot_map, found_exprs, storage_fee


def build_storage_contract_record(
    *,
    creation_previous_block_hash: bytes,
    creation_height: int,
    new_size: int,
    new_count: int,
) -> tuple[bytes, list[Expr]]:
    record = StorageRecord(
        creation_block_hash=creation_previous_block_hash,
        last_payment_block_hash=creation_previous_block_hash,
        last_payment_height=creation_height - 1,
        last_payment_winner=ZERO32,
        new_size=new_size,
        new_count=new_count,
    )
    return record.expr().hash(), [record.expr()]


def handle_storage_initial_contract(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
    sender_account: Any,
    storage_account: Any,
    expr_list_id: bytes,
    current_fees: int,
) -> int | None:
    """Handle a storage-initial contract transaction and return charged storage fee."""
    try:
        existing_record = get_from_radix_tree(storage_account.data, node, expr_list_id)
        if existing_record is not None:
            return None

        if transaction is not None:
            tx_exprs, missed = resolve_inner_exprs(node, transaction.expr())
            if missed:
                return None
            data_head = transaction.data._head
            if data_head is None or data_head._tag != "link":
                return None
            list_expr = data_head._head
            if list_expr is None or list_expr._tag != "link":
                return None
            list_items, missed = resolve_list_exprs(node, list_expr)
            if missed:
                return None
        else:
            list_expr = get_expr_list(node, expr_list_id)
            if list_expr is None:
                return None
            list_items, _ = resolve_list_exprs(node, list_expr)
        total_bytes = sum(item.size() for item in list_items)
        number_of_exprs = len(list_items)

        storage_cost = calculate_storage_fee(block, total_bytes)
        if sender_account.balance < current_fees + storage_cost:
            return None

        record_value, record_exprs = build_storage_contract_record(
            creation_previous_block_hash=block.previous_block_hash,
            creation_height=block.height,
            new_size=total_bytes,
            new_count=number_of_exprs,
        )

        put_in_radix_tree(storage_account.data, node, expr_list_id, record_value)
        storage_account.data_hash = storage_account.data.root_hash
        storage_account.balance += storage_cost
        sender_account.balance -= storage_cost

        block.pending_exprs.extend(record_exprs)
        return storage_cost
    except Exception:
        return None
