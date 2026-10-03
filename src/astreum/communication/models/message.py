import os
import struct
from enum import IntEnum
from time import time
from typing import Optional
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from astreum.crypto import chacha20poly1305

class MessageTopic(IntEnum):
    PING = 0
    STORAGE_REQUEST = 1
    STORAGE_RESPONSE = 2
    ROUTE_REQUEST = 3
    ROUTE_RESPONSE = 4
    TRANSACTION = 5
    BLOCK = 6


class Message:
    def __init__(
        self,
        *,
        handshake: bool = False,
        sender: Optional[X25519PublicKey] = None,
        topic: Optional[MessageTopic] = None,
        content: Optional[bytes] = None,
        body: Optional[bytes] = None,
        sender_public_key_bytes: Optional[bytes] = None,
        encrypted: Optional[bytes] = None,
        timestamp: Optional[int] = None,
    ) -> None:
        if body is not None:
            if content is not None and content != b"":
                raise ValueError("specify only one of 'content' or 'body'")
            content = body

        self.handshake = handshake
        self.topic = topic
        self.content = content if content is not None else b""
        self.encrypted = encrypted
        self.timestamp = int(time()) if timestamp is None else int(timestamp)

        if self.handshake:
            if sender_public_key_bytes is None and sender is None:
                raise ValueError("handshake Message requires a sender public key or sender public key bytes")
            self.topic = None
        else:
            if self.topic is None and self.encrypted is None:
                raise ValueError("non-handshake Message requires a topic or encrypted payload")
            if sender_public_key_bytes is None and sender is None:
                raise ValueError("non-handshake Message requires a sender public key or sender public key bytes")

        if sender_public_key_bytes is not None:
            self.sender_public_key_bytes = sender_public_key_bytes
        else:
            if sender is None:
                raise ValueError("sender public key required to derive sender public key bytes")
            self.sender_public_key_bytes = sender.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )

    def to_bytes(self):
        if self.handshake:
            # handshake byte (1) + raw public key bytes + payload
            return bytes([1]) + self.sender_public_key_bytes + self.content
        else:
            # normal message: 0 + sender + encrypted payload (nonce + ciphertext)
            if not self.encrypted:
                raise ValueError("non-handshake Message missing encrypted payload; call encrypt() first")
            return bytes([0]) + self.sender_public_key_bytes + self.encrypted

    @classmethod
    def from_bytes(cls, data: bytes) -> "Message":
        if len(data) < 1:
            raise ValueError("Cannot parse Message: no data")

        # 1 byte type + 32 bytes sender = 33 bytes min
        if len(data) < 33:
            raise ValueError("Cannot parse Message: missing header bytes")

        if data[0] == 1:
            return Message(
                handshake=True,
                sender_public_key_bytes=data[1:33],
                content=data[33:],
            )

        else:
            if len(data) <= 33:
                raise ValueError("Cannot parse Message: missing encrypted payload")

            return Message(
                handshake=False,
                sender_public_key_bytes=data[1:33],
                encrypted=data[33:],
            )

    def encrypt(self, shared_key_bytes: bytes) -> None:
        if self.handshake:
            return
        
        if len(shared_key_bytes) != 32:
            raise ValueError("Shared key must be 32 bytes for ChaCha20-Poly1305")
        
        if self.topic is None:
            raise ValueError("Cannot encrypt message without a topic")

        nonce = os.urandom(12)
        data_to_encrypt = struct.pack(">Q", self.timestamp) + bytes([self.topic.value]) + self.content
        ciphertext = chacha20poly1305.encrypt(shared_key_bytes, nonce, data_to_encrypt)
        self.encrypted = nonce + ciphertext

    def decrypt(self, shared_key_bytes: bytes) -> None:
        if self.handshake:
            return
        
        if len(shared_key_bytes) != 32:
            raise ValueError("Shared key must be 32 bytes for ChaCha20-Poly1305")
        
        if not self.encrypted or len(self.encrypted) < 13:
            raise ValueError("Encrypted content missing or too short")

        nonce = self.encrypted[:12]
        ciphertext = self.encrypted[12:]
        decrypted = chacha20poly1305.decrypt(shared_key_bytes, nonce, ciphertext)
        if len(decrypted) < 9:
            raise ValueError("Decrypted content missing timestamp or topic")
        (self.timestamp,) = struct.unpack(">Q", decrypted[:8])
        topic_value = decrypted[8]
        self.topic = MessageTopic(topic_value)
        self.content = decrypted[9:]
