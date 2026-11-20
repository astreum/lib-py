"""Guards that the fixed-term treasury machinery is gone.

Plan 2 replaces fixed-term annuity loans with perpetual loans. These tests
fail while any part of the old machinery is still reachable: the discount
module, the schedule-shaped helpers in ``treasury/utils.py``, or the removed
record fields anywhere under ``src/``.
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.transaction.treasury import utils as treasury_utils

TREASURY_DIR = SRC_DIR / "astreum" / "consensus" / "transaction" / "treasury"
DISCOUNT_MODULE = "astreum.consensus.transaction.treasury.discount"
TREASURY_PACKAGE = "astreum.consensus.transaction.treasury"
REMOVED_RECORD_FIELDS = (
    "payment_interval_blocks",
    "payment_count",
    "next_payment_block_number",
)
REMOVED_HELPERS = (
    "_loan_payment_count",
    "_paid_payment_count",
    "_remaining_payment_count",
    "_interest_paid_delta",
)


def _source_files():
    return sorted(SRC_DIR.rglob("*.py"))


def _source_imports_discount(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == DISCOUNT_MODULE or alias.name.startswith(DISCOUNT_MODULE + "."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == DISCOUNT_MODULE or module.startswith(DISCOUNT_MODULE + "."):
                return True
            for alias in node.names:
                if alias.name != "discount":
                    continue
                if module == TREASURY_PACKAGE or node.level:
                    return True
    return False


def _imports_discount(path):
    return _source_imports_discount(path.read_text(encoding="utf-8"))


class TestDiscountModuleRemoved(unittest.TestCase):
    def test_discount_module_file_is_gone(self):
        self.assertFalse(
            (TREASURY_DIR / "discount.py").exists(),
            "treasury/discount.py is dead code and must be deleted",
        )

    def test_no_source_module_imports_discount(self):
        offenders = [
            str(path.relative_to(SRC_DIR))
            for path in _source_files()
            if _imports_discount(path)
        ]
        self.assertEqual(offenders, [], "modules still import the removed discount module")


class TestDiscountImportDetection(unittest.TestCase):
    def test_detects_absolute_package_import(self):
        self.assertTrue(
            _source_imports_discount(
                "from astreum.consensus.transaction.treasury import discount\n"
            )
        )

    def test_detects_relative_package_import(self):
        self.assertTrue(_source_imports_discount("from . import discount\n"))

    def test_ignores_relative_import_of_another_name(self):
        self.assertFalse(_source_imports_discount("from . import records\n"))

    def test_ignores_treasury_package_import_of_another_name(self):
        self.assertFalse(
            _source_imports_discount(
                "from astreum.consensus.transaction.treasury import utils\n"
            )
        )


class TestFixedTermHelpersRemoved(unittest.TestCase):
    def test_utils_exposes_no_payment_count_helpers(self):
        present = [name for name in REMOVED_HELPERS if hasattr(treasury_utils, name)]
        self.assertEqual(present, [], "treasury/utils.py still defines fixed-term helpers")


class TestRemovedRecordFieldsAbsent(unittest.TestCase):
    def test_removed_record_fields_appear_nowhere_in_src(self):
        offenders = []
        for path in _source_files():
            text = path.read_text(encoding="utf-8")
            for field in REMOVED_RECORD_FIELDS:
                if field in text:
                    offenders.append("%s: %s" % (path.relative_to(SRC_DIR), field))
        self.assertEqual(offenders, [], "removed fixed-term record fields remain under src/")


if __name__ == "__main__":
    unittest.main()
