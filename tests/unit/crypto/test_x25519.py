"""Unit tests for astreum.crypto.x25519 key generation and key exchange."""
import unittest

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from astreum.crypto.x25519 import generate_key_pair, generate_shared_key


class TestX25519(unittest.TestCase):

    def test_generate_key_pair(self):
        private_key, public_key = generate_key_pair()
        self.assertIsInstance(private_key, X25519PrivateKey)
        self.assertIsInstance(public_key, X25519PublicKey)

    def test_shared_key_roundtrip(self):
        sk_alice, pk_alice = generate_key_pair()
        sk_bob, pk_bob = generate_key_pair()
        shared_alice = generate_shared_key(sk_alice, pk_bob)
        shared_bob = generate_shared_key(sk_bob, pk_alice)
        self.assertEqual(shared_alice, shared_bob)
        self.assertEqual(len(shared_alice), 32)

    def test_asymmetric_keys_differ(self):
        sk1, _ = generate_key_pair()
        sk2, _ = generate_key_pair()
        _, pk_a = generate_key_pair()
        _, pk_b = generate_key_pair()
        shared1 = generate_shared_key(sk1, pk_a)
        shared2 = generate_shared_key(sk2, pk_b)
        self.assertNotEqual(shared1, shared2)


if __name__ == "__main__":
    unittest.main()
