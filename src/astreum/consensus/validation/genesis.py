from __future__ import annotations

from typing import Any

from astreum.consensus.constants import STORAGE_ADDRESS, TREASURY_ADDRESS
from astreum.expression import resolve_inner_exprs
from astreum.consensus.account import create_account
from astreum.consensus.transaction.treasury.record import (
    TreasuryUserRecord,
)
from astreum.consensus.models.accounts import Accounts
from astreum.consensus.models.block import Block
from astreum.consensus.block.create import create_block
from astreum.expression import ZERO32
from astreum.storage.radix import RadixTree, put_in_radix_tree
from time import time


def create_genesis_block(
    node: Any,
    validator_public_key: bytes,
    chain_id: int = 0,
) -> Block:
    validator_pk = validator_public_key

    if len(validator_pk) != 32:
        raise ValueError("validator_public_key must be 32 bytes")

    stake_trie = RadixTree()
    treasury_record = TreasuryUserRecord(balance=1)
    treasury_record_head = treasury_record.expr()
    put_in_radix_tree(stake_trie, node, validator_pk, treasury_record_head)
    stake_root = stake_trie.root_hash or ZERO32

    treasury_account = create_account(balance=1, data_hash=stake_root, counter=0)
    treasury_account.data = stake_trie
    treasury_account.data_hash = stake_root
    storage_account = create_account(balance=0, data_hash=ZERO32, counter=0)
    validator_account = create_account(balance=0, data_hash=ZERO32, counter=0)

    accounts = Accounts()
    treasury_record_exprs, _ = resolve_inner_exprs(node, treasury_record.expr())
    accounts.pending_exprs.extend(treasury_record_exprs)
    accounts.set_account(TREASURY_ADDRESS, treasury_account)
    accounts.set_account(STORAGE_ADDRESS, storage_account)
    accounts.set_account(validator_pk, validator_account)

    accounts_root = accounts.update_trie(node)
    if accounts_root is None:
        raise ValueError("genesis accounts trie is empty")

    block = create_block(
        chain_id=chain_id,
        previous_block_hash=ZERO32,
        previous_block=None,
        height=0,
        timestamp=int(time()),
        accounts_hash=accounts_root,
        total_transaction_fee=0,
        total_storage_fee=0,
        statistics=[(1, 1, 0, 0)],
        transactions_hash=ZERO32,
        receipts_hash=ZERO32,
        difficulty=0,
        validator_public_key_bytes=validator_pk,
        nonce=0,
        signature=b"",
        accounts=accounts,
        transactions=[],
        receipts=[],
    )

    return block
