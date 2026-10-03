from __future__ import annotations

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
) -> Account:
    return Account(
        balance=balance,
        code_hash=code_hash,
        counter=counter,
        data_hash=data_hash,
        channels_hash=channels_hash,
        data=RadixTree(root_hash=data_hash, contact=contact),
        channels=RadixTree(root_hash=channels_hash, contact=contact),
    )
