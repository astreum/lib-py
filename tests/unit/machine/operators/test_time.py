import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.machine.main import Machine
from astreum.machine.parser import parse
from astreum.machine.tokenizer import tokenize
from astreum.expression import NIL

import unittest
from astreum.expression.helpers import get_expr_builtin_tag


class TestTimeOperator(unittest.TestCase):
    def setUp(self):
        self.machine = Machine(node=None)

    def test_time_returns_int(self):
        expr, _ = parse(tokenize("(time)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertIsInstance(result.value, int)

    def test_time_approx_now(self):
        before = int(time.time())
        expr, _ = parse(tokenize("(time)"))
        result = self.machine.run(expr=expr)
        after = int(time.time())
        self.assertGreaterEqual(result.value, before)
        self.assertLessEqual(result.value, after + 1)

    def test_clock_returns_int(self):
        expr, _ = parse(tokenize("(clock)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertIsInstance(result.value, int)

    def test_clock_approx_now(self):
        before = time.perf_counter_ns()
        expr, _ = parse(tokenize("(clock)"))
        result = self.machine.run(expr=expr)
        after = time.perf_counter_ns()
        self.assertGreaterEqual(result.value, before)
        self.assertLessEqual(result.value, after + 1_000_000)

    def test_clock_is_monotonic(self):
        expr, _ = parse(tokenize("(clock)"))
        first = self.machine.run(expr=expr)
        second = self.machine.run(expr=expr)
        self.assertGreaterEqual(second.value, first.value)


class TestTimeDeterministic(unittest.TestCase):
    def setUp(self):
        self.machine = Machine(node=None, mode="deterministic")

    def test_time_returns_nil(self):
        expr, _ = parse(tokenize("(time)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    def test_clock_returns_nil(self):
        expr, _ = parse(tokenize("(clock)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)


if __name__ == "__main__":
    unittest.main()
