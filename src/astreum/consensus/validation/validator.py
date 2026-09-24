from __future__ import annotations

import random
from dataclasses import replace
from typing import Any, Dict, Optional, Tuple

from astreum.expression import resolve_inner_exprs
from astreum.storage.radix import get_all_from_radix_tree, get_from_radix_tree, put_in_radix_tree
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.account import create_account
from astreum.consensus.transaction.treasury.loans import (
    apply_treasury_loan_payments_from_stake_return,
)
from astreum.consensus.transaction.treasury.record import (
    TreasuryUserRecord,
)
from astreum.consensus.models.accounts import Accounts
from astreum.consensus.block.encoding.decode import get_block_from_storage
from astreum.expression import ZERO32


SLOT_DURATION_SECONDS = 2


def current_validator(
    node: Any,
    block_hash: bytes,
    target_time: Optional[int] = None,
) -> Tuple[bytes, Accounts]:
    block = get_block_from_storage(astreum_node=node, block_hash=block_hash)

    if block.timestamp is None:
        raise ValueError("block timestamp missing")

    block_timestamp = block.timestamp
    target_timestamp = int(target_time) if target_time is not None else block_timestamp + 1
    if target_timestamp <= block_timestamp:
        target_timestamp = block_timestamp + 1

    accounts_hash = getattr(block, "accounts_hash", None)
    if not accounts_hash:
        raise ValueError("block missing accounts hash")
    accounts = Accounts(root_hash=accounts_hash)

    treasury_account = accounts.get_account(TREASURY_ADDRESS, node)
    if treasury_account is None:
        raise ValueError("treasury account missing from accounts trie")

    stake_trie = treasury_account.data

    stakes: Dict[bytes, int] = {}
    treasury_user_records: Dict[bytes, TreasuryUserRecord] = {}
    for account_key, record_head in get_all_from_radix_tree(stake_trie, node).items():
        if not account_key:
            continue
        if not record_head or record_head == ZERO32:
            continue
        record = TreasuryUserRecord.from_storage(node, record_head.hash())
        if record is None:
            continue
        stakes[account_key] = record.balance
        treasury_user_records[account_key] = record

    if not stakes:
        raise ValueError("no validator stakes found in treasury trie")

    seed_value = int.from_bytes(bytes(block_hash or getattr(block, "previous_block_hash", ZERO32)), "big", signed=False)
    rng = random.Random(seed_value)

    def pick_validator() -> bytes:
        positive_weights = [(key, weight) for key, weight in stakes.items() if weight > 0]
        if not positive_weights:
            raise ValueError("no validators with positive stake")
        total_weight = sum(weight for _, weight in positive_weights)
        choice = rng.randrange(total_weight)
        cumulative = 0
        for key, weight in sorted(positive_weights, key=lambda item: item[0]):
            cumulative += weight
            if choice < cumulative:
                return key
        return positive_weights[-1][0]

    def halve_stake(validator_key: bytes) -> None:
        current_amount = stakes.get(validator_key, 0)
        if current_amount <= 0:
            raise ValueError("validator stake must be positive")
        new_amount = current_amount // 2
        if new_amount < 1:
            new_amount = 1
        returned_amount = current_amount - new_amount
        if returned_amount <= 0:
            return

        record = treasury_user_records[validator_key]
        if record.loans_root_hash == ZERO32:
            updated_record = replace(record, balance=new_amount)
            updated_record_head = updated_record.expr().hash()
            put_in_radix_tree(stake_trie, node, validator_key, updated_record_head)
            record_exprs, _ = resolve_inner_exprs(node, updated_record.expr())
            accounts.pending_exprs.extend(record_exprs)
            treasury_account.data_hash = stake_trie.root_hash or ZERO32
            treasury_account.balance -= returned_amount

            validator_account = accounts.get_account(validator_key, node)
            if validator_account is None:
                validator_account = create_account()
            validator_account.balance += returned_amount
            accounts.set_account(validator_key, validator_account)
            stakes[validator_key] = new_amount
            treasury_user_records[validator_key] = updated_record
        else:
            if not apply_treasury_loan_payments_from_stake_return(
                node=node,
                accounts=accounts,
                borrower=validator_key,
                amount=returned_amount,
            ):
                raise ValueError("failed applying treasury loan payment from stake return")

            updated_record_head = get_from_radix_tree(treasury_account.data, node, validator_key)
            updated_record = TreasuryUserRecord.from_storage(
                node,
                updated_record_head or ZERO32,
            )
            if updated_record is None:
                raise ValueError("validator treasury record missing after loan payment")
            stakes[validator_key] = updated_record.balance
            treasury_user_records[validator_key] = updated_record

        accounts.set_account(TREASURY_ADDRESS, treasury_account)

    delta = target_timestamp - block_timestamp
    slots_to_process = max(1, (delta + SLOT_DURATION_SECONDS - 1) // SLOT_DURATION_SECONDS)

    selected_validator = pick_validator()
    halve_stake(selected_validator)
    for _ in range(1, slots_to_process):
        selected_validator = pick_validator()
        halve_stake(selected_validator)

    return selected_validator, accounts
