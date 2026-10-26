"""Block verify fetches what is new in the parent with batched requests."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.consensus.account.model import ACCOUNT_SHAPE  # noqa: E402
from astreum.consensus.block.verify import transactions as verify_module  # noqa: E402
from astreum.consensus.block.verify.transactions import verify_block_transactions  # noqa: E402
from astreum.consensus.transaction.storage.model import STORAGE_RECORD_SHAPE  # noqa: E402
from astreum.expression import link_list_to_expr  # noqa: E402

CONTACT = object()


class TestVerifyBatch(unittest.TestCase):
    def _run(self, data_root, fetch_side_effect=None):
        """Run verify up to the record. Return (result, calls in order)."""
        calls = mock.Mock()
        prev_block = SimpleNamespace(
            accounts_hash=os.urandom(32), bloom_hash=None, expr_id=os.urandom(32), height=3
        )
        block = SimpleNamespace(
            expr_id=os.urandom(32),
            transactions_hash=link_list_to_expr([]).hash(),
            receipts_hash=os.urandom(32),
            accounts_hash=os.urandom(32),
            previous_block_hash=prev_block.expr_id,
            previous_block=prev_block,
            validator_public_key_bytes=os.urandom(32),
            chain_id=0,
            height=4,
            timestamp=1234,
            difficulty=1,
            contact=CONTACT,
        )
        data = SimpleNamespace(root_hash=data_root, root_block=b"\x0d" * 32)
        storage_account = SimpleNamespace(data=data, data_hash=None)
        accounts = mock.Mock()
        accounts.get_account.return_value = storage_account
        work_block = SimpleNamespace(accounts=accounts, bloom_tree=None)
        node = SimpleNamespace(logger=mock.Mock())

        def fetch(*args, **kwargs):
            calls.fetch(*args, **kwargs)
            if fetch_side_effect is not None:
                raise fetch_side_effect

        def validator(*args, **kwargs):
            calls.validator(*args, **kwargs)
            return block.validator_public_key_bytes, accounts

        def record(*args, **kwargs):
            calls.record()
            raise RuntimeError("stop after the record")

        with mock.patch.object(verify_module, "get_radix_all_from_network", side_effect=fetch), \
             mock.patch.object(verify_module, "get_expr_list", return_value=None), \
             mock.patch.object(verify_module, "current_validator", side_effect=validator), \
             mock.patch.object(verify_module, "create_block", return_value=work_block), \
             mock.patch.object(verify_module, "BloomTree"), \
             mock.patch.object(verify_module, "get_block_expr"), \
             mock.patch.object(verify_module, "generate_initial_storage_record", side_effect=record):
            try:
                result = verify_block_transactions(node, block)
            except RuntimeError:
                result = "reached the record"
        return result, calls, prev_block, data

    def test_accounts_fetch_comes_before_current_validator(self):
        _, calls, prev_block, _ = self._run(b"\x05" * 32)
        names = [c[0] for c in calls.mock_calls]
        self.assertLess(names.index("fetch"), names.index("validator"))
        first = calls.fetch.call_args_list[0]
        self.assertEqual(first.args[1:3], (prev_block.accounts_hash, ACCOUNT_SHAPE))
        self.assertEqual(first.kwargs["block_hash"], prev_block.expr_id)
        self.assertIs(first.kwargs["contact"], CONTACT)
        self.assertIs(first.kwargs["follow_refs"], False)

    def test_scheduled_validator_is_drawn_for_the_time_of_the_block(self):
        _, calls, _, _ = self._run(b"\x05" * 32)
        self.assertEqual(calls.validator.call_args.kwargs["target_time"], 1234)
        self.assertIs(calls.validator.call_args.kwargs["contact"], CONTACT)

    def test_storage_fetch_uses_the_storage_data_root_and_block(self):
        result, calls, _, data = self._run(b"\x05" * 32)
        self.assertEqual(result, "reached the record")
        second = calls.fetch.call_args_list[1]
        self.assertEqual(second.args[1:3], (data.root_hash, STORAGE_RECORD_SHAPE))
        self.assertEqual(second.kwargs["block_hash"], data.root_block)
        self.assertIs(second.kwargs["follow_refs"], False)
        names = [c[0] for c in calls.mock_calls]
        self.assertLess(len(names) - 1 - names[::-1].index("fetch"), names.index("record"))

    def test_an_empty_storage_trie_sends_no_request(self):
        for empty in (None, b"", b"\x00" * 32):
            with self.subTest(empty=empty):
                _, calls, _, _ = self._run(empty)
                self.assertEqual(calls.fetch.call_count, 1)

    def test_a_failed_fetch_does_not_fail_verification(self):
        result, calls, _, _ = self._run(b"\x05" * 32, fetch_side_effect=ValueError("boom"))
        self.assertEqual(result, "reached the record")
        self.assertEqual(calls.fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
