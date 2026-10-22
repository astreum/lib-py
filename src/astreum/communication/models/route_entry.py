"""Wire format of the entries in a ``ROUTE_RESPONSE``.

An entry names one peer. The layout and the codec are in ``peer_info.py``.
The host is 4 bytes for IPv4 and 16 bytes for IPv6.
"""

from __future__ import annotations

from astreum.communication.message_pow import MESSAGE_FRAMING_BYTES
from astreum.communication.models.peer_info import peer_info_size

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
    return peer_info_size(use_ipv6)


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
