import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, "src")

from astreum.communication.models.route import Route

SELF_KEY = b"\x00" * 32


def _key(first: int, last: int = 0) -> bytes:
    return bytes([first]) + b"\x00" * 30 + bytes([last])


def _route(keys, bucket_size=16):
    route = Route(SELF_KEY, bucket_size=bucket_size)
    for key in keys:
        route.add_peer(key, SimpleNamespace(public_key_bytes=key, address=("127.0.0.1", 1)))
    return route


class TestCountNearer(unittest.TestCase):
    def test_empty_route(self):
        self.assertEqual(Route(SELF_KEY).count_nearer(_key(0x10), _key(0x20), 2), 0)

    def test_counts_strictly_nearer(self):
        route = _route([_key(0x11), _key(0x80)])
        # Target 0x10: key 0x20 is at distance 0x30. 0x11 is nearer, 0x80 is farther.
        self.assertEqual(route.count_nearer(_key(0x10), _key(0x20), 5), 1)

    def test_equal_distance_not_counted(self):
        route = _route([_key(0x20)])
        self.assertEqual(route.count_nearer(_key(0x10), _key(0x20), 5), 0)

    def test_skips_key(self):
        route = _route([_key(0x20), _key(0x11)])
        self.assertEqual(route.count_nearer(_key(0x10), _key(0x20), 5), 1)
        self.assertEqual(
            route.count_nearer(_key(0x10), _key(0x20), 5, extra=[_key(0x20)]), 1
        )

    def test_extra_keys(self):
        route = _route([])
        self.assertEqual(
            route.count_nearer(_key(0x10), _key(0x20), 5, extra=[_key(0x11), _key(0x80)]), 1
        )

    def test_no_double_count(self):
        route = _route([_key(0x11)])
        self.assertEqual(
            route.count_nearer(_key(0x10), _key(0x20), 5, extra=[_key(0x11)]), 1
        )

    def test_stops_at_limit(self):
        route = _route([_key(0x11), _key(0x12), _key(0x13), _key(0x14), _key(0x15)])
        self.assertEqual(route.count_nearer(_key(0x10), _key(0x20), 2), 2)


class TestClosestPeers(unittest.TestCase):
    def test_order(self):
        route = _route([_key(0x80), _key(0x10), _key(0x40)])
        keys = [p.public_key_bytes for p in route.closest_peers(SELF_KEY, 3)]
        self.assertEqual(keys, [_key(0x10), _key(0x40), _key(0x80)])

    def test_count(self):
        route = _route([_key(0x80, i) for i in range(10)])
        self.assertEqual(len(route.closest_peers(SELF_KEY, 4)), 4)
        self.assertEqual(len(route.closest_peers(SELF_KEY, 40)), 10)

    def test_skips_missing_peer(self):
        route = _route([_key(0x10), _key(0x20)])
        del route.peers[_key(0x10)]
        keys = [p.public_key_bytes for p in route.closest_peers(SELF_KEY, 5)]
        self.assertEqual(keys, [_key(0x20)])


if __name__ == "__main__":
    unittest.main()
