import unittest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from astreum.communication.models.message import Message, MessageTopic

class TestMessageWireFormat(unittest.TestCase):
    def setUp(self):
        self.private_key = x25519.X25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()
        self.sender_bytes = self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )

    def test_handshake_message_roundtrip(self):
        msg = Message(
            handshake=True,
            sender=self.public_key,
            content=b"hello"
        )

        encoded = msg.to_bytes()
        # 1 byte type + 32 bytes sender + 5 bytes content = 38 bytes
        self.assertEqual(len(encoded), 38)
        self.assertEqual(encoded[0], 1)

        decoded = Message.from_bytes(encoded)
        self.assertTrue(decoded.handshake)
        self.assertEqual(decoded.content, b"hello")
        self.assertEqual(decoded.sender_public_key_bytes, self.sender_bytes)

    def test_normal_message_roundtrip(self):
        encrypted_payload = b"nonce+ciphertext"

        msg = Message(
            handshake=False,
            sender=self.public_key,
            topic=MessageTopic.PING,
            encrypted=encrypted_payload,
        )

        encoded = msg.to_bytes()
        # 1 byte type + 32 bytes sender + encrypted
        self.assertEqual(len(encoded), 1 + 32 + len(encrypted_payload))
        self.assertEqual(encoded[0], 0)

        decoded = Message.from_bytes(encoded)
        self.assertFalse(decoded.handshake)
        self.assertEqual(decoded.encrypted, encrypted_payload)
        self.assertEqual(decoded.sender_public_key_bytes, self.sender_bytes)

    def test_invalid_short_length(self):
        data = bytes([1]) + self.sender_bytes  # only 33 bytes, no payload
        # Should be parseable (min 33 bytes, content can be empty)
        decoded = Message.from_bytes(data)
        self.assertTrue(decoded.handshake)
        self.assertEqual(decoded.content, b"")

    def test_invalid_too_short(self):
        data = bytes([1]) + self.sender_bytes[:31]  # 32 bytes total
        with self.assertRaisesRegex(ValueError, "missing header bytes"):
            Message.from_bytes(data)

if __name__ == '__main__':
    unittest.main()
