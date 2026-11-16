"""A block that carries a transaction below the bid floor is rejected."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.unit.consensus.transaction.test_signature import TestBlockVerify, _signed


class TestBidFloor(unittest.TestCase):
    def test_a_zero_bid_is_rejected(self):
        key = Ed25519PrivateKey.generate()
        result, apply = TestBlockVerify()._run([_signed(key, base_fee=0)])
        self.assertIsNotNone(result)
        ok, reason = result
        self.assertFalse(ok)
        self.assertIn("invalid base fee", reason)
        apply.assert_not_called()

    def test_a_one_aster_bid_is_accepted(self):
        key = Ed25519PrivateKey.generate()
        result, apply = TestBlockVerify()._run([_signed(key, base_fee=1)])
        apply.assert_called_once()


if __name__ == "__main__":
    unittest.main()
