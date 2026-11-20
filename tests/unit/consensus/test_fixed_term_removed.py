"""Guards that removed treasury machinery stays gone.

Plan 2 replaces fixed-term annuity loans with perpetual loans. Plan C deletes
the guarantee apparatus in full and removes the over-built insurance fund
ledger: premiums are treasury income. These tests fail while any part of the
old machinery is still reachable: the discount module, the schedule-shaped
helpers in ``treasury/utils.py``, the guarantee module and record, the fund
module and record, or the removed record fields anywhere under ``src/``.
"""

import ast
import dataclasses
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
GUARANTEE_MODULE = "astreum.consensus.transaction.treasury.guarantees"
FUND_MODULE = "astreum.consensus.transaction.treasury.fund"
TREASURY_PACKAGE = "astreum.consensus.transaction.treasury"
REMOVED_FUND_NAMES = (
    "TreasuryFundRecord",
    "TREASURY_FUND_KEY",
    "collect_insurance_premium",
    "load_fund_record",
    "write_fund_record",
    "premiums_in",
    "write_offs_out",
    "realized_write_off_rate",
)
REMOVED_RECORD_FIELDS = (
    "payment_interval_blocks",
    "payment_count",
    "next_payment_block_number",
    # The deleted guarantor capacity field: a bare name census, so a
    # reintroduced field of any shape is caught.
    "guaranteed",
)
REMOVED_HELPERS = (
    "_loan_payment_count",
    "_paid_payment_count",
    "_remaining_payment_count",
    "_interest_paid_delta",
)
# Every name the deleted guarantee apparatus was reachable by. A hit means
# live guarantee code survived, not an English use of the word.
REMOVED_GUARANTEE_NAMES = (
    "TreasuryGuarantee",
    "claimed_guarantees",
    "guarantees_root_hash",
    "guarantees_root_block",
    "guarantee_refs",
    "guarantee_amount",
    "guarantee_transaction_id",
    "claim_guarantee",
    "handle_treasury_guarantee",
    "_release_guarantees",
    "TREASURY_GUARANTEE",
)


def _source_files():
    return sorted(SRC_DIR.rglob("*.py"))


def _imports_module(source, module_name, attribute):
    """True when *source* imports *attribute* from *module_name*.

    Catches ``from <module_name> import <attribute>``, a star-free relative
    form of it, and the package form ``from <treasury package> import
    <attribute>``.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name or alias.name.startswith(module_name + "."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == module_name or module.startswith(module_name + "."):
                return True
            for alias in node.names:
                if alias.name != attribute:
                    continue
                if module == TREASURY_PACKAGE or node.level:
                    return True
    return False


def _source_imports_discount(source):
    return _imports_module(source, DISCOUNT_MODULE, "discount")


def _source_imports_guarantees(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == GUARANTEE_MODULE or alias.name.startswith(
                    GUARANTEE_MODULE + "."
                ):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == GUARANTEE_MODULE or module.startswith(GUARANTEE_MODULE + "."):
                return True
            if (module == TREASURY_PACKAGE or node.level) and any(
                alias.name == "guarantees" for alias in node.names
            ):
                return True
    return False


def _source_imports_fund(source):
    return _imports_module(source, FUND_MODULE, "collect_insurance_premium")


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


def _field_references(source: str) -> set[str]:
    """Return the names *source* uses as identifiers or exact string keys.

    A record field reaches the code as a name (annotation, assignment,
    attribute, keyword argument, parameter), or as an exact string key. English
    prose in a docstring is not a field reference, so it does not count.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


class TestRemovedRecordFieldsAbsent(unittest.TestCase):
    def test_removed_record_fields_appear_nowhere_in_src(self):
        offenders = []
        for path in _source_files():
            references = _field_references(path.read_text(encoding="utf-8"))
            for field in REMOVED_RECORD_FIELDS:
                if field in references:
                    offenders.append("%s: %s" % (path.relative_to(SRC_DIR), field))
        self.assertEqual(offenders, [], "removed record fields remain a name under src/")

    def test_detects_a_reintroduced_field_in_every_form(self):
        self.assertIn("guaranteed", _field_references("guaranteed: int = 0\n"))
        self.assertIn("guaranteed", _field_references("record = TreasuryUserRecord(guaranteed=5)\n"))
        self.assertIn("guaranteed", _field_references("total = record.guaranteed\n"))
        self.assertIn("guaranteed", _field_references('slot = record["guaranteed"]\n'))

    def test_ignores_the_word_in_prose(self):
        self.assertNotIn(
            "guaranteed", _field_references('"""Round-trip is guaranteed."""\n')
        )


class TestGuaranteeModuleRemoved(unittest.TestCase):
    def test_guarantee_module_file_is_gone(self):
        self.assertFalse(
            (TREASURY_DIR / "guarantees.py").exists(),
            "treasury/guarantees.py is dead code and must be deleted",
        )

    def test_no_source_module_imports_the_guarantee_module(self):
        offenders = [
            str(path.relative_to(SRC_DIR))
            for path in _source_files()
            if _source_imports_guarantees(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [], "modules still import the removed guarantee module")

    def test_detects_an_absolute_guarantee_import(self):
        self.assertTrue(
            _source_imports_guarantees(
                "from astreum.consensus.transaction.treasury.guarantees "
                "import claim_guarantee\n"
            )
        )

    def test_detects_a_relative_guarantee_import(self):
        self.assertTrue(_source_imports_guarantees("from . import guarantees\n"))


class TestGuaranteeApparatusRemoved(unittest.TestCase):
    def test_removed_guarantee_names_appear_nowhere_in_src(self):
        offenders = []
        for path in _source_files():
            text = path.read_text(encoding="utf-8")
            for name in REMOVED_GUARANTEE_NAMES:
                if name in text:
                    offenders.append("%s: %s" % (path.relative_to(SRC_DIR), name))
        self.assertEqual(
            offenders, [], "removed guarantee names remain under src/"
        )

    def test_the_guarantee_record_type_is_gone(self):
        from astreum.consensus.transaction.treasury import record

        self.assertFalse(
            hasattr(record, "TreasuryGuarantee"),
            "TreasuryGuarantee must not exist",
        )

    def test_the_guarantee_transaction_has_no_handler(self):
        from astreum.consensus.transaction import apply as apply_module

        self.assertFalse(
            hasattr(apply_module, "handle_treasury_guarantee"),
            "the apply path must not import a guarantee handler",
        )

    def test_the_guarantee_transaction_code_is_gone(self):
        from astreum.consensus.transaction.code import TransactionCode

        self.assertNotIn(
            "TREASURY_GUARANTEE",
            TransactionCode.__members__,
            "the guarantee transaction code must be deleted",
        )

    def test_the_guarantee_transaction_code_does_not_decode(self):
        # 0x24 is no longer a known code, so decoding fails closed instead of
        # producing a transaction some handler could silently accept.
        from astreum.consensus.transaction.code import transaction_code_from_bytes

        with self.assertRaises(ValueError):
            transaction_code_from_bytes(b"\x24")

    def test_the_loan_record_carries_no_guarantee_field(self):
        from astreum.consensus.transaction.treasury.record import TreasuryLoanRecord

        field_names = [
            field.name
            for field in dataclasses.fields(TreasuryLoanRecord)
            if not field.name.startswith("_")
        ]
        self.assertNotIn("claimed_guarantees", field_names)
        self.assertEqual(
            field_names,
            [
                "creation_block_number",
                "loan_type",
                "discounted_amount",
                "payment_amount",
                "paid_until_block",
                "insurance_interval_fee",
                "missed_count",
                "owner",
            ],
        )

    def test_the_user_record_carries_no_guarantee_field(self):
        from astreum.consensus.transaction.treasury.record import TreasuryUserRecord

        field_names = [
            field.name
            for field in dataclasses.fields(TreasuryUserRecord)
            if not field.name.startswith("_")
        ]
        self.assertEqual(
            field_names,
            [
                "balance",
                "loans_root_hash",
                "total_interest_paid",
                "loaned",
                "defaulted",
                "loans_root_block",
            ],
            "the user record keeps no guarantee trie and no guaranteed capacity",
        )

    def test_create_transaction_takes_no_guarantee_parameter(self):
        import inspect

        from astreum.consensus.transaction import create_transaction

        parameters = inspect.signature(create_transaction).parameters
        for name in ("guarantee_amount", "guarantee_refs", "price", "expiry"):
            self.assertNotIn(name, parameters, "create_transaction still takes %s" % name)


class TestFundApparatusRemoved(unittest.TestCase):
    """The design owner's ruling: there is no fund. Premiums are income."""

    def test_the_fund_module_file_is_gone(self):
        self.assertFalse(
            (TREASURY_DIR / "fund.py").exists(),
            "treasury/fund.py is dead code and must be deleted",
        )

    def test_no_source_module_imports_the_fund_module(self):
        offenders = [
            str(path.relative_to(SRC_DIR))
            for path in _source_files()
            if _source_imports_fund(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [], "modules still import the removed fund module")

    def test_the_fund_record_type_is_gone(self):
        from astreum.consensus.transaction.treasury import record

        self.assertFalse(
            hasattr(record, "TreasuryFundRecord"),
            "TreasuryFundRecord must not exist",
        )

    def test_the_fund_key_is_gone(self):
        from astreum.consensus import constants

        self.assertFalse(
            hasattr(constants, "TREASURY_FUND_KEY"),
            "TREASURY_FUND_KEY must not exist",
        )

    def test_no_source_names_the_fund_ledger(self):
        offenders = []
        for path in _source_files():
            references = _field_references(path.read_text(encoding="utf-8"))
            for name in REMOVED_FUND_NAMES:
                if name in references:
                    offenders.append("%s: %s" % (path.relative_to(SRC_DIR), name))
        self.assertEqual(offenders, [], "the deleted fund ledger is still named under src/")


if __name__ == "__main__":
    unittest.main()
