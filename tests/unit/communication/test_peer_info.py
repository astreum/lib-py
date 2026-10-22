"""Tests for the PeerInfo codec."""
import socket
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.communication.models.peer_info import (
    PEER_INFO_BYTES,
    PeerInfo,
    decode_peer_info,
    decode_peer_infos,
    encode_peer_info,
    peer_info_size,
)


def _info(n: int = 1, host: str = "10.0.0.1", port: int = 5000) -> PeerInfo:
    return PeerInfo(bytes([n]) * 32, bytes([n + 1]) * 32, (host, port))


def _payload(n: int, port: int = 5000) -> bytes:
    return (
        bytes([n]) * 32
        + bytes([n + 1]) * 32
        + socket.inet_aton(f"10.0.0.{n}")
        + port.to_bytes(2, "big")
    )


class TestSize(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(peer_info_size(False), 70)
        self.assertEqual(peer_info_size(True), 82)
        self.assertEqual(PEER_INFO_BYTES, 70)


class TestEncode(unittest.TestCase):
    def test_ipv4_round_trip(self):
        data = encode_peer_info(_info(1, "10.1.2.3", 6000), False)
        self.assertEqual(len(data), 70)
        info = decode_peer_info(data, False)
        self.assertEqual(info.storage_key, bytes([1]) * 32)
        self.assertEqual(info.relay_key, bytes([2]) * 32)
        self.assertEqual(info.address, ("10.1.2.3", 6000))

    def test_ipv6_round_trip(self):
        data = encode_peer_info(_info(1, "2001:db8::1", 6000), True)
        self.assertEqual(len(data), 82)
        info = decode_peer_info(data, True)
        self.assertEqual(info.address, ("2001:db8::1", 6000))

    def test_encode_matches_old_layout(self):
        data = encode_peer_info(_info(3, "10.0.0.3", 5000), False)
        self.assertEqual(data, _payload(3))

    def test_wrong_family_raises(self):
        with self.assertRaises(ValueError):
            encode_peer_info(_info(1, "2001:db8::1"), False)
        with self.assertRaises(ValueError):
            encode_peer_info(_info(1, "10.0.0.1"), True)

    def test_bad_host_raises(self):
        for host in ("not-a-host", "", None):
            with self.assertRaises(ValueError, msg=str(host)):
                encode_peer_info(_info(1, host), False)

    def test_bad_port_raises(self):
        for port in (65536, -1):
            with self.assertRaises(ValueError, msg=str(port)):
                encode_peer_info(_info(1, port=port), False)

    def test_bad_key_length_raises(self):
        with self.assertRaises(ValueError):
            encode_peer_info(PeerInfo(b"\x01" * 31, b"\x02" * 32, ("10.0.0.1", 1)), False)
        with self.assertRaises(ValueError):
            encode_peer_info(PeerInfo(b"\x01" * 32, b"\x02" * 33, ("10.0.0.1", 1)), False)

    def test_port_zero_and_max(self):
        for port in (0, 65535):
            data = encode_peer_info(_info(1, port=port), False)
            self.assertEqual(decode_peer_info(data, False).address[1], port)


class TestDecode(unittest.TestCase):
    def test_decode_exact_size(self):
        for size in (69, 71):
            with self.assertRaises(ValueError, msg=str(size)):
                decode_peer_info(b"\x01" * size, False)
        self.assertEqual(decode_peer_info(_payload(1), False).address, ("10.0.0.1", 5000))

    def test_decode_exact_size_ipv6(self):
        with self.assertRaises(ValueError):
            decode_peer_info(_payload(1), True)

    def test_decode_infos_one_and_two(self):
        [one] = decode_peer_infos(_payload(1), False)
        self.assertEqual(one.storage_key, bytes([1]) * 32)
        two = decode_peer_infos(_payload(1) + _payload(2, 6000), False)
        self.assertEqual([i.storage_key for i in two], [bytes([1]) * 32, bytes([2]) * 32])
        self.assertEqual(two[1].address, ("10.0.0.2", 6000))

    def test_decode_infos_bad_length_raises(self):
        for size in (0, 69, 100):
            with self.assertRaises(ValueError, msg=str(size)):
                decode_peer_infos(b"\x01" * size, False)


if __name__ == "__main__":
    unittest.main()
