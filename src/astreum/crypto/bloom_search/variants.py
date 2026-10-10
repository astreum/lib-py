from __future__ import annotations

from astreum.expression import ZERO32


def make_search_variants(tx_hash: bytes, sender: bytes,
                         receiver: bytes, key: bytes = ZERO32) -> list[bytes]:
    """Build 7 base variants or 4 tx-forced key-including variants.

    Each variant is 160 bytes. The fifth column holds the involved account.
    This function always leaves it zero, so a contract cannot forge it.

    Args:
        tx_hash: Transaction hash.
        sender: Sender address.
        receiver: Receiver address.
        key: Data key.

    Returns:
        list[bytes]: The 160-byte variants.
    """
    z = b"\x00" * 32
    tx_hash = tx_hash or z
    sender = sender or z
    receiver = receiver or z
    key = key or z
    if key == z:
        return [
            tx_hash + z + z + z + z,
            z + sender + z + z + z,
            z + z + receiver + z + z,
            tx_hash + sender + z + z + z,
            tx_hash + z + receiver + z + z,
            z + sender + receiver + z + z,
            tx_hash + sender + receiver + z + z,
        ]
    return [
        tx_hash + z + z + key + z,
        tx_hash + sender + z + key + z,
        tx_hash + z + receiver + key + z,
        tx_hash + sender + receiver + key + z,
    ]


def make_involved_variants(tx_hash: bytes, account: bytes) -> list[bytes]:
    """Build the two elements that mark an account as involved.

    Args:
        tx_hash (bytes): Transaction hash.
        account (bytes): Address of the involved account.

    Returns:
        list[bytes]: The block element ``0|0|0|0|account`` first, then the
            transaction element ``tx_hash|0|0|0|account``. Both are 160 bytes.
    """
    z = b"\x00" * 32
    tx_hash = tx_hash or z
    return [
        z + z + z + z + account,
        tx_hash + z + z + z + account,
    ]
