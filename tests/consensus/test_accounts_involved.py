"""Tests for Accounts.involved and its snapshot/restore."""
import os
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from astreum.consensus.account import create_account
from astreum.consensus.models.accounts import Accounts
from astreum.consensus.models.block import Block


def _block():
    block = Block.__new__(Block)
    block.accounts = Accounts()
    block.pending_exprs = []
    block.global_loaned = 0
    block.global_defaulted = 0
    block.global_loan_count = 0
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


if __name__ == "__main__":
    unittest.main()
