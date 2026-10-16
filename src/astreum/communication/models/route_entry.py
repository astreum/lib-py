"""Wire format of the entries in a ``ROUTE_RESPONSE``.

An entry names one peer: ``storage_key(32) + relay_key(32) + host + port(2)``.
The host is 4 bytes for IPv4 and 16 bytes for IPv6.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

from astreum.communication.message_pow import MESSAGE_FRAMING_BYTES

if TYPE_CHECKING:
    from astreum.communication.models.peer import Peer

KEY_BYTES = 32
PORT_BYTES = 2
ROUTE_ID_BYTES = 1

# A datagram of this size is never IP-fragmented (see DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES).
ROUTE_REPLY_MAX_BYTES = 1232


def route_entry_size(use_ipv6: bool) -> int:
    """Return the size of one encoded route entry.

    Args:
        use_ipv6: True if the node uses IPv6 addresses.

    Returns:
        70 for IPv4 or 82 for IPv6.
    """
    return 2 * KEY_BYTES + (16 if use_ipv6 else 4) + PORT_BYTES


def max_route_entries(use_ipv6: bool, bucket_size: int) -> int:
    """Return how many entries one ``ROUTE_RESPONSE`` datagram holds.

    Args:
        use_ipv6: True if the node uses IPv6 addresses.
        bucket_size: The bucket size of the route.

    Returns:
        The smaller of ``bucket_size`` and the count that fits in the datagram.
    """
    room = ROUTE_REPLY_MAX_BYTES - MESSAGE_FRAMING_BYTES - ROUTE_ID_BYTES
    return max(0, min(bucket_size, room // route_entry_size(use_ipv6)))


def encode_route_entry(peer: "Peer", use_ipv6: bool) -> bytes | None:
    """Encode a peer as a route entry.

    Args:
        peer: The peer to encode.
        use_ipv6: True if the node uses IPv6 addresses.

    Returns:
        The encoded entry, or None if the peer has no address of the node's
        address family.
    """
    if peer.address is None:
        return None
    host, port = peer.address
    family = socket.AF_INET6 if use_ipv6 else socket.AF_INET
    try:
        host_bytes = socket.inet_pton(family, host)
        port_bytes = int(port).to_bytes(PORT_BYTES, "big", signed=False)
    except (OSError, ValueError, OverflowError):
        return None
    return peer.public_key_bytes + peer.relay_public_key_bytes + host_bytes + port_bytes


def decode_route_entries(
    payload: bytes, use_ipv6: bool
) -> list[tuple[bytes, bytes, tuple[str, int]]]:
    """Decode the entries of a ``ROUTE_RESPONSE``.

    Args:
        payload: The entries, without the route id byte.
        use_ipv6: True if the node uses IPv6 addresses.

    Returns:
        A list of ``(storage_key, relay_key, (host, port))``. An entry with an
        invalid host is left out.

    Raises:
        ValueError: If the payload length is not a multiple of the entry size.
    """
    size = route_entry_size(use_ipv6)
    if len(payload) % size != 0:
        raise ValueError("route entries have a bad length")
    host_len = 16 if use_ipv6 else 4
    family = socket.AF_INET6 if use_ipv6 else socket.AF_INET
    entries = []
    for start in range(0, len(payload), size):
        chunk = payload[start : start + size]
        host_at = 2 * KEY_BYTES
        try:
            host = socket.inet_ntop(family, chunk[host_at : host_at + host_len])
        except (OSError, ValueError):
            continue
        port = int.from_bytes(chunk[host_at + host_len :], "big", signed=False)
        entries.append((chunk[:KEY_BYTES], chunk[KEY_BYTES : 2 * KEY_BYTES], (host, port)))
    return entries
