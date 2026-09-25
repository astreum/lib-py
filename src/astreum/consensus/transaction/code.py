from __future__ import annotations

from enum import IntEnum


class TransactionCode(IntEnum):
    TRANSFER = 0x00

    CHANNEL_UPDATE = 0x10
    CHANNEL_WITHDRAW = 0x11
    CHANNEL_CLOSE = 0x12

    TREASURY_DEPOSIT = 0x20
    TREASURY_BORROW = 0x21
    TREASURY_REPAY = 0x22
    TREASURY_CLOSE = 0x23
    TREASURY_SELL = 0x24
    TREASURY_BUY = 0x25
    TREASURY_CLAIM = 0x26

    STORAGE_CREATE = 0x30
    STORAGE_PAYMENT = 0x31
    STORAGE_REMOVE = 0x32

    CODE_ACCOUNT_CREATE = 0x40
    CODE_ACCOUNT_CALL = 0x41


def transaction_code_to_bytes(code: TransactionCode) -> bytes:
    return int(code).to_bytes(1, "little", signed=False)


def transaction_code_from_bytes(raw: bytes) -> TransactionCode:
    if len(raw) != 1:
        raise ValueError(f"transaction code must be exactly 1 byte, got {len(raw)}")

    try:
        return TransactionCode(raw[0])
    except ValueError as exc:
        raise ValueError(f"unknown transaction code: 0x{raw[0]:02x}") from exc
