"""Wire format of the ping, with and without a holder."""
import sys
import unittest

sys.path.insert(0, "src")

from astreum.communication.models.ping import Ping, PingFormatError
from astreum.communication.models.relay_endpoint import RelayEndpoint

HEAD = b"\x07" * 32
RELAY_KEY = b"\x09" * 32


class TestPingFormat(unittest.TestCase):
    def test_round_trip_with_holder(self):
        holder = RelayEndpoint(RELAY_KEY, ("10.1.2.3", 52780))
        data = Ping(True, 5, HEAD, holder).to_bytes()
        self.assertEqual(len(data), 72)
        ping = Ping.from_bytes(data)
        self.assertTrue(ping.is_validator)
        self.assertEqual(ping.difficulty, 5)
        self.assertEqual(ping.latest_block, HEAD)
        self.assertEqual(ping.holder.relay_key, RELAY_KEY)
        self.assertEqual(ping.holder.address, ("10.1.2.3", 52780))

    def test_round_trip_without_holder(self):
        data = Ping(False, 1, HEAD).to_bytes()
        self.assertEqual(len(data), 72)
        self.assertEqual(data[34:], b"\x00" * 38)
        ping = Ping.from_bytes(data)
        self.assertEqual(ping.latest_block, HEAD)
        self.assertIsNone(ping.holder)

    def test_round_trip_without_head(self):
        ping = Ping.from_bytes(Ping(True, 1, None).to_bytes())
        self.assertIsNone(ping.latest_block)
        self.assertIsNone(ping.holder)

    def test_wrong_length_is_rejected(self):
        good = Ping(True, 1, HEAD).to_bytes()
        for data in (good[:34], good[:71], good + b"\x00"):
            with self.assertRaises(PingFormatError):
                Ping.from_bytes(data)

    def test_contact_without_head_is_rejected(self):
        data = Ping(True, 1, None).to_bytes()[:34] + RELAY_KEY + b"\x0a\x00\x00\x01\x00\x50"
        with self.assertRaises(PingFormatError):
            Ping.from_bytes(data)

    def test_holder_without_head_cannot_be_built(self):
        with self.assertRaises(ValueError):
            Ping(True, 1, None, RelayEndpoint(RELAY_KEY, ("10.1.2.3", 1)))

    def test_holder_with_bad_address_is_rejected_on_encode(self):
        for address in (("::1", 80), ("example.org", 80), ("10.1.2.3", 70000)):
            with self.assertRaises(ValueError):
                Ping(True, 1, HEAD, RelayEndpoint(RELAY_KEY, address)).to_bytes()

    def test_holder_with_short_key_is_rejected_on_encode(self):
        with self.assertRaises(ValueError):
            Ping(True, 1, HEAD, RelayEndpoint(b"\x01" * 31, ("10.1.2.3", 80))).to_bytes()


if __name__ == "__main__":
    unittest.main()
