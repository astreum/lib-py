from __future__ import annotations

from typing import Optional

from astreum.expression import ZERO32
from astreum.storage.radix import RadixTree
from astreum.consensus.account.model import Account


def create_account(
    balance: int = 0,
    data_hash: bytes = ZERO32,
    channels_hash: bytes = ZERO32,
    code_hash: bytes = ZERO32,
    counter: int = 0,
    contact=None,
    data_block: Optional[bytes] = None,
    code_block: Optional[bytes] = None,
    channels_block: Optional[bytes] = None,
) -> Account:
    return Account(
        balance=balance,
        code_hash=code_hash,
        counter=counter,
        data_hash=data_hash,
        channels_hash=channels_hash,
        data=RadixTree(root_hash=data_hash, contact=contact, root_block=data_block),
        channels=RadixTree(root_hash=channels_hash, contact=contact, root_block=channels_block),
        data_block=data_block,
        code_block=code_block,
        channels_block=channels_block,
    )
