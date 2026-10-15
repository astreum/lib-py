import socket
from typing import Tuple


def decode_storage_provider(payload: bytes) -> Tuple[bytes, bytes, str, int]:
    """Decode a 70-byte provider payload.

    Format: Ed25519_storage(32) + X25519_relay(32) + IPv4(4) + port(2)

    Returns (storage_public_key, relay_public_key, address, port).

    Args:
        payload (bytes): The 70-byte provider payload to decode.

    Returns:
        Tuple[bytes, bytes, str, int]: Description of the return value.
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


PROVIDER_PAYLOAD_LEN = 32 + 32 + 4 + 2


def decode_storage_providers(data: bytes) -> list[Tuple[bytes, bytes, str, int]]:
    """Decode concatenated 70-byte provider payloads.

    Args:
        data: One or more provider payloads joined together.

    Returns:
        One ``(storage_key, relay_key, address, port)`` tuple per payload, in
        the order of the data.

    Raises:
        ValueError: If the length is 0 or is not a multiple of 70.
    """
    if not data or len(data) % PROVIDER_PAYLOAD_LEN != 0:
        raise ValueError(
            f"provider payloads must be a multiple of {PROVIDER_PAYLOAD_LEN} bytes "
            f"(got={len(data)})"
        )
    return [
        decode_storage_provider(data[i : i + PROVIDER_PAYLOAD_LEN])
        for i in range(0, len(data), PROVIDER_PAYLOAD_LEN)
    ]
