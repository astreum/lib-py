"""Tests for Accounts.involved and its snapshot/restore."""
import os
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from astreum.consensus.account import create_account
from astreum.consensus.models.accounts import Accounts, compute_total_balance
from astreum.consensus.models.block import Block
from astreum.expression import Expr, NIL, int_, link, bytes_ as expr_bytes
from astreum.storage.radix import put_in_radix_tree


def _block():
    block = Block.__new__(Block)
    block.accounts = Accounts()
    block.pending_exprs = []
    block.total_balance = 0
    block.global_loaned = 0
    block.global_defaulted = 0
    block.global_loan_count = 0
    block.storage_votes = 0
    block.storage_claim_count = 0
    return block


class TestAccountsInvolved(unittest.TestCase):

    def test_get_account_does_not_add(self):
        accounts = Accounts()
        address = os.urandom(32)
        accounts._cache[address] = create_account()
        accounts.get_account(address)
        self.assertEqual(accounts.involved, set())

    def test_set_account_adds(self):
        accounts = Accounts()
        address = os.urandom(32)
        accounts.set_account(address, create_account())
        self.assertEqual(accounts.involved, {address})

    def test_snapshot_restore_drops_address(self):
        block = _block()
        snapshot = block.snapshot()
        address = os.urandom(32)
        block.accounts.set_account(address, create_account())
        block.restore(snapshot)
        self.assertEqual(block.accounts.involved, set())

    def test_snapshot_restore_keeps_earlier_address(self):
        block = _block()
        earlier = os.urandom(32)
        block.accounts.set_account(earlier, create_account())
        snapshot = block.snapshot()
        block.accounts.set_account(os.urandom(32), create_account())
        block.restore(snapshot)
        self.assertEqual(block.accounts.involved, {earlier})

    def test_supply_tracking_seeds_cached_account_baselines(self):
        """Cached accounts must not report their whole balance as a new delta."""
        block = _block()
        address = os.urandom(32)
        account = create_account(balance=100)
        block.accounts._cache[address] = account
        block.accounts.start_supply_tracking(
            lambda delta: setattr(
                block, "total_balance", block.total_balance + delta
            )
        )
        account.balance = 110
        block.accounts.set_account(address, account)
        self.assertEqual(block.total_balance, 10)

        account.balance = 120
        block.accounts.set_account(address, account)
        self.assertEqual(block.total_balance, 20)
        block.accounts.stop_supply_tracking()


class _FakeNode:
    """Minimal in-memory node for supply-scan tests."""

    def __init__(self):
        import threading
        from types import SimpleNamespace
        self.hot_storage = {}
        self.hot_storage_lock = threading.Lock()
        self.hot_storage_timestamps = {}
        self.config = SimpleNamespace(cold_storage_path=None)
        self.is_connected = False
        self.logger = type(
            "FakeLogger",
            (),
            {
                "debug": lambda *a, **kw: None,
                "info": lambda *a, **kw: None,
                "warning": lambda *a, **kw: None,
                "exception": lambda *a, **kw: None,
            },
        )()

    def get_expr(self, expr_id):
        if isinstance(expr_id, Expr):
            return expr_id
        return self.hot_storage.get(expr_id)

    def get_expr_list(self, root_hash):
        if isinstance(root_hash, Expr):
            return root_hash
        return self.hot_storage.get(root_hash)


class TestComputeTotalBalanceRaises(unittest.TestCase):

    def _put_expr(self, account_expr):
        node = _FakeNode()
        accounts = Accounts()
        address = os.urandom(32)
        put_in_radix_tree(accounts._trie, node, address, account_expr)
        return node, accounts

    def _account_expr(self, *elements):
        expr = NIL
        for element in reversed(elements):
            expr = link(element, expr)
        return expr

    def test_valid_account_is_counted(self):
        account = create_account(balance=123)
        node, accounts = self._put_expr(account.to_expr())
        self.assertEqual(compute_total_balance(accounts, node), 123)

    def test_malformed_account_entry_raises(self):
        expr = self._account_expr(int_(1), int_(2))
        node, accounts = self._put_expr(expr)
        with self.assertRaisesRegex(ValueError, "invalid account entry length"):
            compute_total_balance(accounts, node)

    def test_unresolvable_account_entry_raises(self):
        expr = Expr("link", head=int_(1), tail_hash=os.urandom(32))
        node, accounts = self._put_expr(expr)
        with self.assertRaisesRegex(ValueError, "unable to resolve account expression"):
            compute_total_balance(accounts, node)

    def test_non_int_balance_raises(self):
        expr = self._account_expr(
            int_(1), int_(2), int_(3), int_(4), expr_bytes(os.urandom(32))
        )
        node, accounts = self._put_expr(expr)
        with self.assertRaisesRegex(ValueError, "account balance is not an int"):
            compute_total_balance(accounts, node)

    def test_non_list_account_entry_raises(self):
        """The trie only holds 5-element account lists; any other type raises."""
        node = _FakeNode()
        accounts = Accounts()
        put_in_radix_tree(accounts._trie, node, os.urandom(32), int_(42))
        with self.assertRaisesRegex(ValueError, "invalid account entry length"):
            compute_total_balance(accounts, node)


if __name__ == "__main__":
    unittest.main()
