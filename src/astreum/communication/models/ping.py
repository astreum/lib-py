from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class PingFormatError(ValueError):
    """Raised when ping payload bytes are invalid."""


@dataclass
class Ping:
    is_validator: bool
    latest_block: Optional[bytes]

    MIN_PAYLOAD_SIZE = 1
    FULL_PAYLOAD_SIZE = 33

    def __post_init__(self) -> None:
        if self.latest_block is None:
            return
        lb = bytes(self.latest_block)
        if len(lb) != 32:
            raise ValueError("latest_block must be exactly 32 bytes")
        self.latest_block = lb

    def to_bytes(self) -> bytes:
        flag = b"\x01" if self.is_validator else b"\x00"
        if self.latest_block is None:
            return flag
        return flag + self.latest_block

    @classmethod
    def from_bytes(cls, data: bytes) -> "Ping":
        if len(data) == cls.MIN_PAYLOAD_SIZE:
            flag = data[0]
            if flag not in (0, 1):
                raise PingFormatError("ping validator flag must be 0 or 1")
            return cls(is_validator=bool(flag), latest_block=None)
        if len(data) != cls.FULL_PAYLOAD_SIZE:
            raise PingFormatError("ping payload must be 1 or 33 bytes")
        flag = data[0]
        if flag not in (0, 1):
            raise PingFormatError("ping validator flag must be 0 or 1")
        return cls(is_validator=bool(flag), latest_block=data[1:])
