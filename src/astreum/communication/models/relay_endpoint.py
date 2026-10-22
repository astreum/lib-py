"""The relay key and address of a node that can receive an encrypted message."""

from __future__ import annotations


class RelayEndpoint:
    """Where to send an encrypted message.

    The sender derives a shared key from ``relay_key`` and sends the datagram
    to ``address``. A ``RelayEndpoint`` is not a ``PeerInfo``. A ``PeerInfo``
    also holds a storage key.

    Attributes:
        relay_key (bytes): X25519 relay public key (32 bytes).
        address (tuple[str, int]): The ``(host, port)`` of the node.
    """

    relay_key: bytes
    address: tuple[str, int]

    def __init__(self, relay_key: bytes, address: tuple[str, int]) -> None:
        """Create an endpoint.

        Args:
            relay_key (bytes): X25519 relay public key (32 bytes).
            address (tuple[str, int]): The ``(host, port)`` of the node.
        """
        self.relay_key = relay_key
        self.address = address
