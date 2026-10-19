"""Signed handshake content: build and verify.

A handshake proves that the sender owns the storage id in its header and
that the relay key in its content belongs to that storage id.

Content layout (105 bytes)::

    reply_requested(1) + relay_key(32) + timestamp(8) + signature(64)

The signature covers ``storage_id + reply_requested + relay_key + timestamp``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from astreum.communication.models.message import Message
from astreum.crypto.ed25519 import verify_signature

if TYPE_CHECKING:
    from astreum import Node

REPLY_REQUESTED_YES = 0x01
REPLY_REQUESTED_NO = 0x00

HANDSHAKE_REPLY_REQUESTED_START = 0
HANDSHAKE_REPLY_REQUESTED_END = 1
HANDSHAKE_RELAY_KEY_START = 1
HANDSHAKE_RELAY_KEY_END = 33
HANDSHAKE_TIMESTAMP_START = 33
HANDSHAKE_TIMESTAMP_END = 41
HANDSHAKE_SIGNATURE_START = 41
HANDSHAKE_SIGNATURE_END = 105
HANDSHAKE_CONTENT_SIZE = 105


def handshake_signed_bytes(
    storage_id: bytes,
    reply_requested_byte: bytes,
    relay_key: bytes,
    timestamp: int,
) -> bytes:
    """Return the bytes that the handshake signature covers.

    Args:
        storage_id (bytes): Storage public key of the sender (32 bytes).
        reply_requested_byte (bytes): One byte, 0x01 or 0x00.
        relay_key (bytes): X25519 relay public key of the sender (32 bytes).
        timestamp (int): Handshake time in unix seconds.

    Returns:
        bytes: The 73 bytes to sign or verify.
    """
    return storage_id + reply_requested_byte + relay_key + timestamp.to_bytes(8, "big")


def build_handshake(
    node: "Node",
    reply_requested: bool,
    now: float | None = None,
) -> Message:
    """Build a signed handshake message.

    Args:
        node (Node): The node that sends the handshake.
        reply_requested (bool): True asks a known peer to send a handshake
            back.
        now (float, optional): Time to use as the timestamp. Defaults to the
            current time.

    Returns:
        Message: The signed handshake message.
    """
    timestamp = int(time.time()) if now is None else int(now)
    reply_requested_byte = bytes(
        [REPLY_REQUESTED_YES if reply_requested else REPLY_REQUESTED_NO]
    )
    signed = handshake_signed_bytes(
        node.storage_public_key_bytes,
        reply_requested_byte,
        node.relay_public_key_bytes,
        timestamp,
    )
    signature = node.storage_secret_key.sign(signed)
    return Message(
        handshake=True,
        sender_public_key_bytes=node.storage_public_key_bytes,
        content=(
            reply_requested_byte
            + node.relay_public_key_bytes
            + timestamp.to_bytes(8, "big")
            + signature
        ),
    )


def verify_handshake(
    message: Message,
    now: float,
    window: int,
) -> tuple[bytes, int, bool] | None:
    """Check the length, fields, time and signature of a handshake.

    Args:
        message (Message): The received handshake message.
        now (float): Current time in unix seconds.
        window (int): Allowed time difference in seconds. 0 disables the
            time check.

    Returns:
        tuple[bytes, int, bool] | None: ``(relay_key, timestamp,
            reply_requested)`` if the handshake is valid, otherwise None.
    """
    content = message.content
    if len(content) != HANDSHAKE_CONTENT_SIZE:
        return None

    reply_requested_byte = content[
        HANDSHAKE_REPLY_REQUESTED_START:HANDSHAKE_REPLY_REQUESTED_END
    ]
    relay_key = content[HANDSHAKE_RELAY_KEY_START:HANDSHAKE_RELAY_KEY_END]
    timestamp = int.from_bytes(
        content[HANDSHAKE_TIMESTAMP_START:HANDSHAKE_TIMESTAMP_END], "big"
    )
    signature = content[HANDSHAKE_SIGNATURE_START:HANDSHAKE_SIGNATURE_END]

    if reply_requested_byte not in (
        bytes([REPLY_REQUESTED_YES]),
        bytes([REPLY_REQUESTED_NO]),
    ):
        return None
    if window > 0 and abs(now - timestamp) > window:
        return None

    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            message.sender_public_key_bytes
        )
    except ValueError:
        return None

    signed = handshake_signed_bytes(
        message.sender_public_key_bytes, reply_requested_byte, relay_key, timestamp
    )
    if not verify_signature(public_key, signed, signature):
        return None

    return (
        relay_key,
        timestamp,
        reply_requested_byte == bytes([REPLY_REQUESTED_YES]),
    )
