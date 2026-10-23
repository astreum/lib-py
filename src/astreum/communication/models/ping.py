from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Optional

from astreum.communication.models.relay_endpoint import RelayEndpoint


class PingFormatError(ValueError):
    """Raised when ping payload bytes are invalid."""


@dataclass
class Ping:
    """A ping message.

    Attributes:
        is_validator (bool): True if the sender has a validation key.
        difficulty (int): The proof-of-work difficulty the sender wants.
        latest_block (Optional[bytes]): The head block hash of the sender.
        holder (Optional[RelayEndpoint]): The node that holds the head
            block. None when the sender does not know a holder.
    """

    is_validator: bool
    difficulty: int
    latest_block: Optional[bytes]
    holder: Optional[RelayEndpoint] = None

    HOLDER_SIZE = 38
    PAYLOAD_SIZE = 34 + HOLDER_SIZE
    ZERO_BLOCK = b"\x00" * 32
    ZERO_HOLDER = b"\x00" * HOLDER_SIZE

    def __post_init__(self) -> None:
        if self.difficulty < 1 or self.difficulty > 255:
            raise ValueError("difficulty must be between 1 and 255")
        if self.latest_block is not None and len(self.latest_block) != 32:
            raise ValueError("latest_block must be exactly 32 bytes")
        if self.holder is not None and self.latest_block is None:
            raise ValueError("holder needs a latest_block")

    def to_bytes(self) -> bytes:
        """Encode the ping.

        Returns:
            bytes: 72 bytes. The layout is flag (1), difficulty (1),
            latest_block (32) and holder (38).

        Raises:
            ValueError: If the holder address is not IPv4 or the port is not
                in 0-65535.
        """
        flag = b"\x01" if self.is_validator else b"\x00"
        difficulty = bytes([self.difficulty])
        latest_block = self.latest_block if self.latest_block is not None else self.ZERO_BLOCK
        return flag + difficulty + latest_block + self._holder_bytes()

    def _holder_bytes(self) -> bytes:
        """Encode the holder.

        Returns:
            bytes: ``relay_key + ipv4 + port``, or 38 zero bytes for none.

        Raises:
            ValueError: If the key is not 32 bytes, the host is not IPv4 or
                the port is not in 0-65535.
        """
        if self.holder is None:
            return self.ZERO_HOLDER
        key = self.holder.relay_key
        host, port = self.holder.address
        if len(key) != 32:
            raise ValueError("holder relay key must be exactly 32 bytes")
        if not 0 <= port <= 65535:
            raise ValueError("holder port must be in 0-65535")
        try:
            packed = socket.inet_pton(socket.AF_INET, host)
        except OSError as exc:
            raise ValueError("holder host must be an IPv4 address") from exc
        return key + packed + port.to_bytes(2, "big")

    @classmethod
    def from_bytes(cls, data: bytes) -> "Ping":
        """Decode a ping.

        Args:
            data (bytes): Exactly 72 bytes.

        Returns:
            Ping: The decoded ping.

        Raises:
            PingFormatError: If the length is not 72, a field is out of range,
                or a holder comes with no head block.
        """
        if len(data) != cls.PAYLOAD_SIZE:
            raise PingFormatError(f"ping payload must be {cls.PAYLOAD_SIZE} bytes")
        flag = data[0]
        if flag not in (0, 1):
            raise PingFormatError("ping validator flag must be 0 or 1")
        difficulty = data[1]
        if difficulty < 1:
            raise PingFormatError("ping difficulty must be >= 1")
        latest_block = data[2:34]
        if latest_block == cls.ZERO_BLOCK:
            latest_block = None
        raw_holder = data[34:]
        holder = None
        if raw_holder != cls.ZERO_HOLDER:
            if latest_block is None:
                raise PingFormatError("ping holder needs a latest block")
            holder = RelayEndpoint(
                raw_holder[:32],
                (socket.inet_ntop(socket.AF_INET, raw_holder[32:36]), int.from_bytes(raw_holder[36:], "big")),
            )
        return cls(
            is_validator=bool(flag),
            difficulty=difficulty,
            latest_block=latest_block,
            holder=holder,
        )
