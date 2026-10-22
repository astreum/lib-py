"""Wire format of the contact data of one node.

A route entry and a provider payload use the same layout:
``storage_key(32) + relay_key(32) + host + port(2)``.
The host is 4 bytes for IPv4 and 16 bytes for IPv6.
Provider payloads are always IPv4 and 70 bytes long.
"""

from __future__ import annotations

import socket

KEY_BYTES = 32
PORT_BYTES = 2

# Size of an IPv4 entry. Provider payloads have this size.
PEER_INFO_BYTES = 2 * KEY_BYTES + 4 + PORT_BYTES


class PeerInfo:
    """Contact data of one node.

    A ``PeerInfo`` is not a ``Peer``. A ``Peer`` is a node that completed a
    handshake and has a shared key. A ``PeerInfo`` can name a node that this
    node never contacted.

    Attributes:
        storage_key (bytes): Ed25519 storage public key (32 bytes).
        relay_key (bytes): X25519 relay public key (32 bytes).
        address (tuple[str, int]): The ``(host, port)`` of the node.
    """

    storage_key: bytes
    relay_key: bytes
    address: tuple[str, int]

    def __init__(
        self,
        storage_key: bytes,
        relay_key: bytes,
        address: tuple[str, int],
    ) -> None:
        self.storage_key = storage_key
        self.relay_key = relay_key
        self.address = address


def _host_bytes(use_ipv6: bool) -> int:
    return 16 if use_ipv6 else 4


def _family(use_ipv6: bool) -> int:
    return socket.AF_INET6 if use_ipv6 else socket.AF_INET


def peer_info_size(use_ipv6: bool) -> int:
    """Return the size of one encoded ``PeerInfo``.

    Args:
        use_ipv6 (bool): True if the host is an IPv6 address.

    Returns:
        int: 70 for IPv4 or 82 for IPv6.
    """
    return 2 * KEY_BYTES + _host_bytes(use_ipv6) + PORT_BYTES


def encode_peer_info(info: PeerInfo, use_ipv6: bool) -> bytes:
    """Encode a ``PeerInfo``.

    Args:
        info (PeerInfo): The contact data to encode.
        use_ipv6 (bool): True if the host is an IPv6 address.

    Returns:
        bytes: ``storage_key + relay_key + host + port``.

    Raises:
        ValueError: If a key is not 32 bytes, the host is not an address of
            the chosen family, or the port is not in 0-65535.
    """
    if len(info.storage_key) != KEY_BYTES or len(info.relay_key) != KEY_BYTES:
        raise ValueError("peer info keys must be 32 bytes")
    host, port = info.address
    try:
        host_bytes = socket.inet_pton(_family(use_ipv6), host)
    except (OSError, TypeError) as exc:
        raise ValueError(f"invalid host for the address family: {host}") from exc
    try:
        port_bytes = int(port).to_bytes(PORT_BYTES, "big", signed=False)
    except OverflowError as exc:
        raise ValueError(f"port out of range (0-65535): {port}") from exc
    return info.storage_key + info.relay_key + host_bytes + port_bytes


def decode_peer_info(data: bytes, use_ipv6: bool) -> PeerInfo:
    """Decode one ``PeerInfo``.

    Args:
        data (bytes): Exactly one encoded entry.
        use_ipv6 (bool): True if the host is an IPv6 address.

    Returns:
        PeerInfo: The decoded contact data.

    Raises:
        ValueError: If the length is not the entry size.
    """
    size = peer_info_size(use_ipv6)
    if len(data) != size:
        raise ValueError(f"peer info must be {size} bytes (got={len(data)})")
    host_at = 2 * KEY_BYTES
    host = socket.inet_ntop(_family(use_ipv6), data[host_at : size - PORT_BYTES])
    port = int.from_bytes(data[size - PORT_BYTES :], "big", signed=False)
    return PeerInfo(data[:KEY_BYTES], data[KEY_BYTES:host_at], (host, port))


def decode_peer_infos(data: bytes, use_ipv6: bool) -> list[PeerInfo]:
    """Decode joined entries.

    Args:
        data (bytes): One or more encoded entries joined together.
        use_ipv6 (bool): True if the host is an IPv6 address.

    Returns:
        list[PeerInfo]: One item per entry, in the order of the data.

    Raises:
        ValueError: If the length is 0 or not a multiple of the entry size.
    """
    size = peer_info_size(use_ipv6)
    if not data or len(data) % size != 0:
        raise ValueError(
            f"peer infos must be a multiple of {size} bytes (got={len(data)})"
        )
    return [
        decode_peer_info(data[i : i + size], use_ipv6)
        for i in range(0, len(data), size)
    ]
