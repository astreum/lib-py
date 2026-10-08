from __future__ import annotations

from astreum.expression import ZERO32


def make_search_variants(tx_hash: bytes, sender: bytes,
                         receiver: bytes, key: bytes = ZERO32) -> list[bytes]:
    """
    Build 7 base variants or 4 tx-forced key-including variants.

    Args:
        tx_hash: Transaction hash.
        sender: Sender address.
        receiver: Receiver address.
        key: Data key.
    """
    z = b"\x00" * 32
    tx_hash = tx_hash or z
    sender = sender or z
    receiver = receiver or z
    key = key or z
    if key == z:
        return [
            tx_hash + z + z + z,
            z + sender + z + z,
            z + z + receiver + z,
            tx_hash + sender + z + z,
            tx_hash + z + receiver + z,
            z + sender + receiver + z,
            tx_hash + sender + receiver + z,
        ]
    return [
        tx_hash + z + z + key,
        tx_hash + sender + z + key,
        tx_hash + z + receiver + key,
        tx_hash + sender + receiver + key,
    ]
