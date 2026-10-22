"""Unit tests for astreum.crypto.ed25519 key generation, signing, and verification."""
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from astreum.crypto.ed25519 import generate_key_pair, sign_message, verify_signature


class TestEd25519(unittest.TestCase):

    def test_generate_key_pair(self):
        private_key, public_key = generate_key_pair()
        self.assertIsInstance(private_key, Ed25519PrivateKey)
        self.assertIsInstance(public_key, Ed25519PublicKey)

    def test_sign_and_verify_roundtrip(self):
        private_key, public_key = generate_key_pair()
        message = b"hello astreum"
        signature = sign_message(private_key, message)
        self.assertTrue(verify_signature(public_key, message, signature))

    def test_verify_wrong_message(self):
        private_key, public_key = generate_key_pair()
        signature = sign_message(private_key, b"message A")
        self.assertFalse(verify_signature(public_key, b"message B", signature))

    def test_verify_wrong_key(self):
        sk1, pk1 = generate_key_pair()
        sk2, pk2 = generate_key_pair()
        signature = sign_message(sk1, b"shared message")
        self.assertFalse(verify_signature(pk2, b"shared message", signature))

    def test_verify_tampered_signature(self):
        private_key, public_key = generate_key_pair()
        message = b"tamper me"
        signature = sign_message(private_key, message)
        tampered = bytearray(signature)
        tampered[0] ^= 0xFF
        self.assertFalse(verify_signature(public_key, message, bytes(tampered)))

    def test_empty_message(self):
        private_key, public_key = generate_key_pair()
        signature = sign_message(private_key, b"")
        self.assertTrue(verify_signature(public_key, b"", signature))

    def test_deterministic_signatures(self):
        private_key, _ = generate_key_pair()
        message = b"deterministic"
        sig1 = sign_message(private_key, message)
        sig2 = sign_message(private_key, message)
        self.assertEqual(sig1, sig2)


if __name__ == "__main__":
    unittest.main()
