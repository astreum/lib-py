import socket
from typing import Tuple


def decode_storage_provider(payload: bytes) -> Tuple[bytes, bytes, str, int]:
    """Decode a 70-byte provider payload.

    Format: Ed25519_storage(32) + X25519_relay(32) + IPv4(4) + port(2)

    Returns (storage_public_key, relay_public_key, address, port).

    Args:
        payload (bytes): The 70-byte provider payload to decode.
    """
    expected_len = 32 + 32 + 4 + 2
    if len(payload) < expected_len:
        raise ValueError(
            f"provider payload too short (got={len(payload)}, expected={expected_len})"
        )

    storage_public_key = payload[:32]
    relay_public_key = payload[32:64]
    provider_ip_bytes = payload[64:68]
    provider_port_bytes = payload[68:70]

    provider_address = socket.inet_ntoa(provider_ip_bytes)
    provider_port = int.from_bytes(provider_port_bytes, byteorder="big", signed=False)
    return storage_public_key, relay_public_key, provider_address, provider_port
