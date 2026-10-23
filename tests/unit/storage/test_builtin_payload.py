import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.expression import int_, str_, link, NIL  # noqa: E402
from astreum.expression.expr import Expr, get_expr_head_hash  # noqa: E402
from astreum.expression.helpers import builtin_composite_payload  # noqa: E402
from astreum.storage.exprs import put_expr_in_hot_storage  # noqa: E402
from astreum.storage.exprs.hot import get_expr_from_hot_storage  # noqa: E402
from astreum.utils.config import NodeConfig


class _FakeNode:
    def __init__(self):
        import threading
        self.config = NodeConfig(hot_storage_limit=1 << 20)
        self.hot_storage = {}
        self.hot_storage_timestamps = {}
        self.hot_storage_size = 0
        self.hot_storage_lock = threading.RLock()


class TestBuiltinPayload(unittest.TestCase):
    def test_helper_returns_encoded_payload(self):
        payload = builtin_composite_payload(int_(7))
        self.assertEqual(payload.base, "bytes")
        self.assertEqual(payload.hash(), get_expr_head_hash(int_(7)))

    def test_helper_ignores_other_exprs(self):
        self.assertIsNone(builtin_composite_payload(NIL))
        self.assertIsNone(builtin_composite_payload(link(int_(1), NIL)))
        self.assertIsNone(builtin_composite_payload(Expr("bytes", value=b"x")))

    def test_hot_storage_keeps_payload_of_int_and_str(self):
        node = _FakeNode()
        for expr in (int_(1791721517), str_("abc"), link(int_(3), NIL)):
            put_expr_in_hot_storage(node, expr)
        for value in (int_(1791721517), str_("abc"), int_(3)):
            self.assertIsNotNone(
                get_expr_from_hot_storage(node, get_expr_head_hash(value)),
                f"payload of {value!r} should be in hot storage",
            )


if __name__ == "__main__":
    unittest.main()
