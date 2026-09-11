from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astreum.expression import Expr, link_list_to_expr
from astreum.consensus.transaction.storage.initial import generate_initial_storage_record
from astreum.consensus.transaction.storage.model import StorageRecord, StorageSlot


@dataclass
class PendingStorageContract:
    destination_addr: bytes | None
    key: bytes | None
    sender_addr: bytes | None
    header_id: bytes
    storage_id: bytes
    record: StorageRecord
    slot_entries: list[tuple[bytes, StorageSlot]]
    locked: list[bytes] = field(default_factory=list)
    storage_fee: int = 0
    mint: bool = False


def add_pending_storage_contract(
    node: Any,
    block: object,
    destination_addr: bytes | None,
    key: bytes | None,
    value: Expr,
    mint: bool = False,
) -> int | None:
    """
    Generate a storage contract for *value* and register it in
    *block.pending_storage_contracts*.

    Returns the storage fee of the contract on success, or ``None`` if
    ``generate_initial_storage_record`` failed.
    """
    result = generate_initial_storage_record(node, block, value, mint=mint)
    if result is None:
        return None

    record, slot_map, found_exprs, fee = result
    header_id = record.expr().hash()
    storage_id = value.hash()
    slot_entries = [(h, slot) for h, slot in slot_map.items()]

    # For each expr ID found in global storage, lock it on the first
    # earlier contract that introduced it
    for eid in found_exprs:
        for entry in block.pending_storage_contracts:
            entry_ids = {entry.header_id} | {sid for sid, _ in entry.slot_entries}
            if eid in entry_ids:
                if eid not in entry.locked:
                    entry.locked.append(eid)
                break

    block.pending_storage_contracts.append(
        PendingStorageContract(
            destination_addr=destination_addr,
            key=key,
            sender_addr=destination_addr,
            header_id=header_id,
            storage_id=storage_id,
            record=record,
            slot_entries=slot_entries,
            locked=[],
            storage_fee=fee,
            mint=mint,
        )
    )
    return fee


def remove_pending_storage_contract(block, entry):
    """Remove a pending contract and recompute locks for remaining entries."""
    block.pending_storage_contracts.remove(entry)
    _recompute_locked(block.pending_storage_contracts)


def _recompute_locked(pending):
    for entry in pending:
        entry.locked = []
    for later_idx, later in enumerate(pending):
        later_ids = {later.header_id} | {sid for sid, _ in later.slot_entries}
        for eid in later_ids:
            for earlier in pending[:later_idx]:
                earlier_ids = {earlier.header_id} | {sid for sid, _ in earlier.slot_entries}
                if eid in earlier_ids:
                    if eid not in earlier.locked:
                        earlier.locked.append(eid)
                    break


def _write_records_table(node: Any, contracts: list[tuple[bytes, StorageRecord | StorageSlot]]) -> None:
    """Write the records LSM table for the finalized contracts.

    Groups the surviving slots by ``slot.storage_id``, sorts each group by
    ``slot.sequence``, and stores the concat of slot data ids (the ``h``
    values, which are the contract keys for slot entries) under
    ``storage_id``.  Records with no surviving slots get an empty value.
    """
    from astreum.storage.records import put_record_in_cold_storage

    grouped: dict[bytes, list[tuple[int, bytes]]] = {}
    for key, contract in contracts:
        if isinstance(contract, StorageSlot):
            grouped.setdefault(contract.storage_id, []).append(
                (contract.sequence, key)
            )

    for storage_id, seq_ids in grouped.items():
        seq_ids.sort(key=lambda item: item[0])
        slot_ids = [sid for _seq, sid in seq_ids]
        try:
            put_record_in_cold_storage(node, storage_id, slot_ids)
        except Exception:
            node.logger.exception(
                "Records table write failed for record %s", storage_id.hex()
            )


def finalize_pending_storage_contract(
    node: Any,
    block: object,
) -> tuple[
    list[tuple[bytes, StorageRecord | StorageSlot]],
    list[bytes],
    list[tuple[bytes, int]],
]:
    """
    Finalize pending contracts from *block.pending_storage_contracts*:
    survivors go to contracts, overwritten go to deletes + refunds.  For
    locked partial contracts the refund is the difference between the original
    fee and the recalculated fee for the locked subset.
    """
    pending = block.pending_storage_contracts
    seen: set[tuple[bytes, bytes]] = set()
    contracts: list[tuple[bytes, StorageRecord | StorageSlot]] = []
    deletes: list[bytes] = []
    refunds: list[tuple[bytes, int]] = []

    for entry in reversed(pending):
        if entry.key is None:
            # One-shot — always active, no grouping
            contracts.append((entry.storage_id, entry.record))
            for sid, slot in entry.slot_entries:
                contracts.append((sid, slot))
            continue
        dk = (entry.destination_addr, entry.key)
        if dk not in seen:
            # Active entry
            contracts.append((entry.storage_id, entry.record))
            for sid, slot in entry.slot_entries:
                contracts.append((sid, slot))
            seen.add(dk)
        elif entry.locked:
            # Overwritten but has locked dependencies
            if entry.header_id in entry.locked:
                # Full contract preserved — keep everything, no refund
                contracts.append((entry.storage_id, entry.record))
                for sid, slot in entry.slot_entries:
                    contracts.append((sid, slot))
            else:
                deletes.append(entry.header_id)
                # Non-locked old slots → deletes
                for sid, _ in entry.slot_entries:
                    if sid not in entry.locked:
                        deletes.append(sid)
                # Generate new contract from the locked subset
                locked_ids = [
                    eid for eid in entry.locked
                    if eid in {sid for sid, _ in entry.slot_entries}
                ]
                if locked_ids:
                    locked_value = link_list_to_expr(locked_ids)
                    locked_result = generate_initial_storage_record(
                        node, block, locked_value, mint=entry.mint
                    )
                    if locked_result is not None:
                        new_record, new_slot_map, _, new_fee = locked_result
                        new_header_id = new_record.expr().hash()
                        new_storage_id = locked_value.hash()
                        contracts.append((new_storage_id, new_record))
                        for new_sid, new_slot in new_slot_map.items():
                            contracts.append((new_sid, new_slot))
                        refund = entry.storage_fee - new_fee
                        if refund > 0:
                            refunds.append((entry.sender_addr, refund))
        else:
            # Overwritten, no locked IDs
            deletes.append(entry.header_id)
            for sid, _ in entry.slot_entries:
                deletes.append(sid)
            refunds.append((entry.sender_addr, entry.storage_fee))

    _write_records_table(node, contracts)
    return contracts, deletes, refunds
