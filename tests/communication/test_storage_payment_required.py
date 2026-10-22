"""Tests for the STORAGE_PAYMENT_REQUIRED codec and its relay hint."""
import socket
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.communication.models.relay_endpoint import RelayEndpoint
from astreum.communication.storage_response.storage_payment_required import (
    decode_storage_payment_required,
    encode_storage_payment_required,
)

PAY_KEY = b"\x01" * 32
RELAY = b"\x02" * 32


def _encode(hint):
    return encode_storage_payment_required(PAY_KEY, 100, 7, hint)


class TestPaymentRequired(unittest.TestCase):
    def test_no_hint_round_trip(self):
        data = _encode(None)
        self.assertEqual(len(data), 48)
        key, size, fee, hint = decode_storage_payment_required(data)
        self.assertEqual((key, size, fee), (PAY_KEY, 100, 7))
        self.assertIsNone(hint)

    def test_hint_round_trip(self):
        data = _encode(RelayEndpoint(RELAY, ("10.1.2.3", 6000)))
        self.assertEqual(len(data), 86)
        _, _, _, hint = decode_storage_payment_required(data)
        self.assertIsInstance(hint, RelayEndpoint)
        self.assertEqual(hint.relay_key, RELAY)
        self.assertEqual(hint.address, ("10.1.2.3", 6000))

    def test_hint_bytes_match_old_layout(self):
        data = _encode(RelayEndpoint(RELAY, ("10.1.2.3", 6000)))
        self.assertEqual(
            data[48:],
            RELAY + socket.inet_aton("10.1.2.3") + (6000).to_bytes(2, "big"),
        )

    def test_hint_bad_relay_key_raises(self):
        with self.assertRaises(ValueError):
            _encode(RelayEndpoint(b"\x02" * 31, ("10.1.2.3", 6000)))

    def test_hint_bad_host_raises(self):
        for host in ("2001:db8::1", "not-a-host"):
            with self.assertRaises(ValueError, msg=host):
                _encode(RelayEndpoint(RELAY, (host, 6000)))

    def test_hint_bad_port_raises(self):
        with self.assertRaises(ValueError):
            _encode(RelayEndpoint(RELAY, ("10.1.2.3", 65536)))

    def test_decode_bad_length_raises(self):
        for size in (47, 87):
            with self.assertRaises(ValueError, msg=str(size)):
                decode_storage_payment_required(b"\x00" * size)


if __name__ == "__main__":
    unittest.main()
