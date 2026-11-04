from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.communication.disconnect import disconnect_node
from astreum.utils.config import config_setup


class TestDisconnectClaimState(unittest.TestCase):
    def _node(self, **attrs):
        return SimpleNamespace(logger=logging.getLogger("test_disconnect"), **attrs)

    def test_disconnect_returns_claim_state(self):
        node = self._node(
            storage_claim_band=3,
            storage_claim_hash_rate=2.0,
            storage_claim_inclusion_delay=2,
        )
        self.assertEqual(
            disconnect_node(node),
            {
                "storage_claim_band": 3,
                "storage_claim_hash_rate": 2.0,
                "storage_claim_inclusion_delay": 2,
            },
        )
        self.assertFalse(node.is_connected)

    def test_disconnect_missing_attributes_give_none(self):
        self.assertEqual(
            disconnect_node(self._node()),
            {
                "storage_claim_band": None,
                "storage_claim_hash_rate": None,
                "storage_claim_inclusion_delay": None,
            },
        )

    def test_disconnect_state_feeds_config(self):
        node = self._node(
            storage_claim_band=3,
            storage_claim_hash_rate=2.0,
            storage_claim_inclusion_delay=2,
        )
        state = disconnect_node(node)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "accounts").mkdir()
            with mock.patch(
                "astreum.utils.data.ensure_data_dir", return_value=Path(tmp)
            ):
                result = config_setup({"default_seed": None, **state})
        self.assertEqual(result.storage_claim_band, 3)
        self.assertEqual(result.storage_claim_hash_rate, 2.0)
        self.assertEqual(result.storage_claim_inclusion_delay, 2)


if __name__ == "__main__":
    unittest.main()
