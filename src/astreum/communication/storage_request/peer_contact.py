import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astreum.communication.models.peer import Peer


def encode_peer_contact_bytes(peer: "Peer") -> bytes:
    """Return a fixed-width peer contact payload (70 bytes).

    Format: Ed25519_storage(32) + X25519_relay(32) + IPv4(4) + port(2)

    Args:
        peer (Peer): The peer to encode contact information for.
    """
    host, port = peer.address
    storage_key_bytes = peer.public_key_bytes
    relay_key_bytes = peer.relay_public_key_bytes
    try:
        ip_bytes = socket.inet_aton(host)
    except OSError as exc:  # pragma: no cover - inet_aton raises for invalid hosts
        raise ValueError(f"invalid IPv4 address: {host}") from exc
    if not (0 <= port <= 0xFFFF):
        raise ValueError(f"port out of range (0-65535): {port}")
    port_bytes = int(port).to_bytes(2, "big", signed=False)
    return storage_key_bytes + relay_key_bytes + ip_bytes + port_bytes

