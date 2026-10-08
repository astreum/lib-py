import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.machine import Expr, tokenize, parse
from astreum.machine.main import Machine
from astreum.expression import NIL


class TestTxLogOperator(unittest.TestCase):
    def setUp(self):
        self.block = type('Block', (), {})()
        self.machine = Machine(node=None, meter_limit=10_000)
        self.machine.block = self.block
        self.fee_calls = []

        self._patcher = mock.patch(
            'astreum.machine.operators.transaction.log.storage_walk_fee',
            side_effect=self._fake_fee,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def _fake_fee(self, node, block, value):
        self.fee_calls.append(value)
        return value.size() if hasattr(value, 'size') else 1

    def test_bare_pushes_nil(self):
        expr, _ = parse(tokenize("('value tx.log)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result._tag, "link")
        self.assertIsNone(result._head)
        self.assertIsNone(result._tail)

    def test_appends_value_to_logs(self):
        expr, _ = parse(tokenize("('value tx.log)"))
        self.machine.run(expr=expr)
        self.assertEqual(len(self.machine.logs), 1)
        logged = self.machine.logs[0]
        self.assertEqual(logged._tag, "symbol")
        self.assertEqual(logged.value, "value")

    def test_meter_charges_storage(self):
        expr, _ = parse(tokenize("('abcde tx.log)"))
        self.assertEqual(self.machine.meter.storage, 0)
        self.machine.run(expr=expr)
        self.assertEqual(self.machine.meter.storage, 5)

    def test_accumulates_multiple_logs(self):
        for val in ("a", "b", "c"):
            expr, _ = parse(tokenize(f"('{val} tx.log)"))
            self.machine.run(expr=expr)
        self.assertEqual(len(self.machine.logs), 3)
        self.assertEqual(self.machine.logs[0].value, "a")
        self.assertEqual(self.machine.logs[1].value, "b")
        self.assertEqual(self.machine.logs[2].value, "c")

    def test_storage_charge_scales_with_size(self):
        expr_small, _ = parse(tokenize("('ab tx.log)"))
        self.machine.run(expr_small)
        small = self.machine.meter.storage
        expr_large, _ = parse(tokenize("('abcdefghij tx.log)"))
        self.machine.run(expr_large)
        large = self.machine.meter.storage
        self.assertLess(small, large)

    def test_fee_is_computed_once_per_log_and_nothing_is_registered(self):
        expr, _ = parse(tokenize("('value tx.log)"))
        self.machine.run(expr=expr)
        self.assertEqual(len(self.fee_calls), 1)
        self.assertFalse(hasattr(self.block, 'pending_storage_contracts'))

    def test_unsizable_log_fails_the_operator(self):
        with mock.patch(
            'astreum.machine.operators.transaction.log.storage_walk_fee',
            return_value=None,
        ):
            expr, _ = parse(tokenize("('value tx.log)"))
            with self.assertRaises(RuntimeError):
                self.machine.run(expr=expr)
        self.assertEqual(self.machine.logs, [])

    def test_meter_total_reflects_eval_plus_storage(self):
        expr, _ = parse(tokenize("('value tx.log)"))
        self.machine.run(expr=expr)
        self.assertEqual(
            self.machine.meter.total,
            self.machine.meter.eval + self.machine.meter.storage,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)