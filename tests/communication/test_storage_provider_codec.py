"""Tests for decode_storage_providers."""
import socket
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.communication.storage_response.storage_provider import (
    decode_storage_provider,
    decode_storage_providers,
)


def _payload(n: int, port: int = 5000) -> bytes:
    return (
        bytes([n]) * 32
        + bytes([n + 1]) * 32
        + socket.inet_aton(f"10.0.0.{n}")
        + port.to_bytes(2, "big")
    )


class TestDecodeStorageProviders(unittest.TestCase):
    def test_one_piece(self):
        result = decode_storage_providers(_payload(1))
        self.assertEqual(result, [decode_storage_provider(_payload(1))])

    def test_two_pieces(self):
        result = decode_storage_providers(_payload(1) + _payload(2, 6000))
        self.assertEqual(
            result,
            [decode_storage_provider(_payload(1)), decode_storage_provider(_payload(2, 6000))],
        )

    def test_bad_length_raises(self):
        for size in (0, 69, 100):
            with self.assertRaises(ValueError, msg=str(size)):
                decode_storage_providers(b"\x01" * size)


if __name__ == "__main__":
    unittest.main()
