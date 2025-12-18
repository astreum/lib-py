from __future__ import annotations

import random
from typing import Any, Dict, Optional, Tuple

from .genesis import TREASURY_ADDRESS
from .models.account import Account
from .models.accounts import Accounts
from .models.block import Block
from ..storage.models.atom import ZERO32
from ..utils.integer import bytes_to_int, int_to_bytes


SLOT_DURATION_SECONDS = 2


def current_validator(
    node: Any,
    block_hash: bytes,
    target_time: Optional[int] = None,
) -> Tuple[bytes, Accounts]:
    """
    Determine the validator for the requested target_time, halving stakes once per
    slot (currently 2 seconds) between the referenced block and the target time.
    Returns the validator key and the updated accounts snapshot reflecting stake and
    balance adjustments.
    """

    block = Block.from_atom(node, block_hash)

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
    for account_key, stake_amount in stake_trie.get_all(node).items():
        if not account_key:
            continue
        stakes[account_key] = bytes_to_int(stake_amount)

    if not stakes:
        raise ValueError("no validator stakes found in treasury trie")

    seed_source = block_hash or getattr(block, "previous_block_hash", ZERO32)
    seed_value = int.from_bytes(bytes(seed_source), "big", signed=False)
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
        returned_amount = current_amount - new_amount
        stakes[validator_key] = new_amount
        stake_trie.put(node, validator_key, int_to_bytes(new_amount))
        treasury_account.data_hash = stake_trie.root_hash or ZERO32

        validator_account = accounts.get_account(validator_key, node)
        if validator_account is None:
            validator_account = Account.create()
        validator_account.balance += returned_amount
        accounts.set_account(validator_key, validator_account)
        accounts.set_account(TREASURY_ADDRESS, treasury_account)

    delta = target_timestamp - block_timestamp
    slots_to_process = max(1, (delta + SLOT_DURATION_SECONDS - 1) // SLOT_DURATION_SECONDS)

    selected_validator = pick_validator()
    halve_stake(selected_validator)
    for _ in range(1, slots_to_process):
        selected_validator = pick_validator()
        halve_stake(selected_validator)

    return selected_validator, accounts
