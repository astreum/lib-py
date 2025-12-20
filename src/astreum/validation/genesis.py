from __future__ import annotations

from typing import Any, List

from .models.account import Account
from .models.accounts import Accounts
from .models.block import Block
from ..storage.models.atom import ZERO32
from ..storage.models.trie import Trie
from ..utils.integer import int_to_bytes
from time import time

TREASURY_ADDRESS = b"\x01" * 32
BURN_ADDRESS = b"\x00" * 32


def create_genesis_block(
    node: Any,
    validator_public_key: bytes,
    chain_id: int = 0,
) -> Block:
    validator_pk = bytes(validator_public_key)

    if len(validator_pk) != 32:
        raise ValueError("validator_public_key must be 32 bytes")

    stake_trie = Trie()
    stake_amount = int_to_bytes(1)
    stake_trie.put(storage_node=node, key=validator_pk, value=stake_amount)
    stake_root = stake_trie.root_hash or ZERO32

    treasury_account = Account.create(balance=1, data_hash=stake_root, counter=0)
    treasury_account.data = stake_trie
    treasury_account.data_hash = stake_root
    burn_account = Account.create(balance=0, data_hash=b"", counter=0)
    validator_account = Account.create(balance=0, data_hash=b"", counter=0)

    accounts = Accounts()
    accounts.set_account(TREASURY_ADDRESS, treasury_account)
    accounts.set_account(BURN_ADDRESS, burn_account)
    accounts.set_account(validator_pk, validator_account)

    accounts.update_trie(node)
    accounts_root = accounts.root_hash
    if accounts_root is None:
        raise ValueError("genesis accounts trie is empty")

    block = Block(
        chain_id=chain_id,
        previous_block_hash=ZERO32,
        previous_block=None,
        number=0,
        timestamp=int(time()),
        accounts_hash=accounts_root,
        transactions_total_fees=0,
        transactions_hash=ZERO32,
        receipts_hash=ZERO32,
        delay_difficulty=0,
        validator_public_key_bytes=validator_pk,
        nonce=0,
        signature=b"",
        accounts=accounts,
        transactions=[],
        receipts=[],
    )

    return block
