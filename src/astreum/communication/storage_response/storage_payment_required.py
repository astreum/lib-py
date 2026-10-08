import socket
from typing import Optional, Tuple


STORAGE_PAYMENT_REQUIRED_NO_HINT_PAYLOAD_LEN = 32 + 8 + 8
STORAGE_PAYMENT_REQUIRED_WITH_HINT_PAYLOAD_LEN = (
    STORAGE_PAYMENT_REQUIRED_NO_HINT_PAYLOAD_LEN + 32 + 4 + 2
)


def encode_storage_payment_required(
    payment_public_key: bytes,
    storage_size_estimate: int,
    base_storage_fee: int,
    hint_peer: Optional[Tuple[bytes, str, int]] = None,
) -> bytes:
    """Encode payment-required payload with optional IPv4 relay hint.

    Format (no marker byte):
      - no hint: payment_public_key[32] + storage_size_estimate[8] + base_storage_fee[8]
      - with hint: base fields + hint_relay_public_key[32] + hint_ipv4[4] + hint_port[2]

    Args:
        payment_public_key (bytes): The payment public key (32 bytes).
        storage_size_estimate (int): The estimated storage size.
        base_storage_fee (int): The base storage fee.
        hint_peer (Optional[Tuple[bytes, str, int]]): Optional hint peer tuple.

    Returns:
        bytes: The encoded payment-required payload.
    """
    payment_key = payment_public_key
    if len(payment_key) != 32:
        raise ValueError("payment_public_key must be 32 bytes")
    try:
        storage_size_value = int(storage_size_estimate)
    except Exception as exc:
        raise ValueError("storage_size_estimate must be an integer") from exc
    try:
        base_storage_fee_value = int(base_storage_fee)
    except Exception as exc:
        raise ValueError("base_storage_fee must be an integer") from exc
    if not (0 <= storage_size_value <= ((1 << 64) - 1)):
        raise ValueError("storage_size_estimate must fit in u64")
    if not (0 <= base_storage_fee_value <= ((1 << 64) - 1)):
        raise ValueError("base_storage_fee must fit in u64")
    base_payload = (
        payment_key
        + storage_size_value.to_bytes(8, "big", signed=False)
        + base_storage_fee_value.to_bytes(8, "big", signed=False)
    )

    if hint_peer is None:
        return base_payload

    hint_public_key, hint_host, hint_port = hint_peer
    hint_key = hint_public_key
    if len(hint_key) != 32:
        raise ValueError("hint relay public key must be 32 bytes")
    try:
        hint_ip_bytes = socket.inet_aton(hint_host)
    except OSError as exc:
        raise ValueError(f"invalid IPv4 address: {hint_host}") from exc
    if not (0 <= int(hint_port) <= 0xFFFF):
        raise ValueError(f"hint port out of range (0-65535): {hint_port}")

    hint_port_bytes = int(hint_port).to_bytes(2, "big", signed=False)
    return base_payload + hint_key + hint_ip_bytes + hint_port_bytes


def decode_storage_payment_required(
    payload: bytes,
) -> Tuple[bytes, int, int, Optional[Tuple[bytes, str, int]]]:
    """Decode payment-required payload using length to determine optional hint.

    Args:
        payload (bytes): The encoded payload to decode.

    Returns:
        Tuple[bytes, int, int, Optional[Tuple[bytes, str, int]]]: The decoded fields.
    """
    payload_bytes = payload
    if len(payload_bytes) == STORAGE_PAYMENT_REQUIRED_NO_HINT_PAYLOAD_LEN:
        payment_public_key = payload_bytes[:32]
        storage_size_estimate = int.from_bytes(payload_bytes[32:40], "big", signed=False)
        base_storage_fee = int.from_bytes(payload_bytes[40:48], "big", signed=False)
        return payment_public_key, storage_size_estimate, base_storage_fee, None

    if len(payload_bytes) != STORAGE_PAYMENT_REQUIRED_WITH_HINT_PAYLOAD_LEN:
        raise ValueError(
            "invalid STORAGE_PAYMENT_REQUIRED payload length "
            f"({len(payload_bytes)} bytes; expected 48 or 86)"
        )

    payment_public_key = payload_bytes[:32]
    storage_size_estimate = int.from_bytes(payload_bytes[32:40], "big", signed=False)
    base_storage_fee = int.from_bytes(payload_bytes[40:48], "big", signed=False)
    hint_public_key = payload_bytes[48:80]
    hint_ip_bytes = payload_bytes[80:84]
    hint_port_bytes = payload_bytes[84:86]
    hint_address = socket.inet_ntoa(hint_ip_bytes)
    hint_port = int.from_bytes(hint_port_bytes, "big", signed=False)
    return (
        payment_public_key,
        storage_size_estimate,
        base_storage_fee,
        (hint_public_key, hint_address, hint_port),
    )
