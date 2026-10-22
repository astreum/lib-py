"""Tests for involved-account bloom elements in apply_transaction."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
HELPERS_DIR = ROOT / "tests" / "unit" / "consensus" / "validation"
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from astreum.consensus.account import create_account
from astreum.consensus.block.rate import calculate_storage_fee
from astreum.consensus.constants import STORAGE_ADDRESS, TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction import apply_transaction, create_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.apply import make_involved_variants as _real_variants
from astreum.consensus.transaction.treasury.record import LoanType, TreasuryLoanRecord
from astreum.expression import NIL
from astreum.machine.parser import parse
from astreum.machine.tokenizer import tokenize

from _helpers import (
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_program,
    seed_sender_account,
    seed_storage_account,
    seed_treasury_account,
    seed_user_with_loan,
    store_tx,
)

Z = b"\x00" * 32


def _parse(program):
    expr, _ = parse(tokenize(program))
    return expr


def _new_to(*addresses):
    """A program that calls each contract address with tx.new.

    A nested TRANSFER does not run today: `_get_or_create_recipient_account` is
    defined only in the top-level branch of `_apply_tx_effects`. A nested call
    does run, so the tests use CODE_ACCOUNT_CALL (65).
    """
    calls = " ".join(f"65 0x{a.hex()} 0 '() tx.new drop" for a in addresses)
    return _parse(f"({calls})")


def _seed_third(block, node, address=None):
    """A contract that does nothing, to be the target of a nested call."""
    address = address or os.urandom(32)
    code_hash = seed_program(node, _parse("(1 drop)"))
    block.accounts.set_account(address, create_account(balance=0, code_hash=code_hash))
    return address


class _Base(unittest.TestCase):

    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self._use_block(make_block(self.node, self.prev_block))
        seed_storage_account(self.block)

    def _use_block(self, block):
        """Switch to *block* and record every element inserted into its tree.

        A bloom filter has false positives, so the tests read the inserted
        elements and not the filter.
        """
        self.block = block
        self.inserted = []
        original = block.bloom_tree.insert

        def record(offset, elements, *a, **kw):
            self.inserted.extend(elements)
            return original(offset, elements, *a, **kw)

        block.bloom_tree.insert = record

    def _seed_contract(self, program, *, address=None, balance=100_000):
        code_hash = seed_program(self.node, program)
        address = address or os.urandom(32)
        self.block.accounts.set_account(
            address, create_account(balance=balance, code_hash=code_hash)
        )
        return address

    def _call(self, sender_pk, sender_key, recipient, *, counter=0, cost_limit=3000):
        tx = create_transaction(
            chain_id=1, counter=counter, sender=sender_pk, recipient=recipient,
            amount=0, code=TransactionCode.CODE_ACCOUNT_CALL,
            data=NIL, secret_key=sender_key, cost_limit=cost_limit,
        )
        tx_hash = store_tx(self.node, tx)
        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)
        return tx_hash, self.block.receipts[-1]

    def _in_tree(self, element):
        return element in self.inserted


class TestInvolved(_Base):

    def test_plain_transfer_adds_no_elements(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        recipient = os.urandom(32)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=recipient,
            amount=1000, code=TransactionCode.TRANSFER, secret_key=sender_key,
        )
        apply_transaction(self.node, self.block, store_tx(self.node, tx))
        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)
        self.assertEqual(self.block.involved_accounts, set())

    def test_nested_recipient_is_involved(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        third = _seed_third(self.block, self.node)
        contract = self._seed_contract(_new_to(third))
        tx_hash, receipt = self._call(sender_pk, sender_key, contract)
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        block_element, tx_element = _real_variants(tx_hash, third)
        self.assertTrue(self._in_tree(block_element))
        self.assertTrue(self._in_tree(tx_element))
        self.assertEqual(self.block.involved_accounts, {third})

    def test_sender_and_recipient_excluded(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        # The contract pays the sender. The sender is also its caller.
        contract = self._seed_contract(_parse("(0 tx.sender 1000 '() tx.new drop)"))
        tx_hash, receipt = self._call(sender_pk, sender_key, contract)
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(self.block.involved_accounts, set())
        for account in (sender_pk, contract):
            self.assertFalse(self._in_tree(_real_variants(tx_hash, account)[1]))

    def test_storage_and_treasury_excluded(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        contract = self._seed_contract(_new_to(os.urandom(32)))
        tx_hash, receipt = self._call(sender_pk, sender_key, contract)
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        for account in (STORAGE_ADDRESS, TREASURY_ADDRESS):
            self.assertNotIn(account, self.block.involved_accounts)
        # STORAGE_ADDRESS is 32 zero bytes. Its block element would be all zero.
        self.assertNotIn(Z * 5, self.inserted)
        self.assertFalse(self._in_tree(_real_variants(tx_hash, TREASURY_ADDRESS)[1]))

    def test_failed_tx_adds_no_elements(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        third = _seed_third(self.block, self.node)
        contract = self._seed_contract(_new_to(third))
        # A wrong counter fails the transaction before the bloom stage.
        tx_hash, receipt = self._call(sender_pk, sender_key, contract, counter=7)
        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(self.block.involved_accounts, set())
        self.assertFalse(self._in_tree(_real_variants(tx_hash, third)[1]))

    def test_failed_call_adds_no_elements(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        third = _seed_third(self.block, self.node)
        contract = self._seed_contract(_new_to(third))
        # Too little to run the program: the call fails and rolls back.
        tx_hash, receipt = self._call(sender_pk, sender_key, contract, cost_limit=1)
        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(self.block.involved_accounts, set())
        self.assertFalse(self._in_tree(_real_variants(tx_hash, third)[1]))

    def test_involved_cleared_per_tx(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        third = _seed_third(self.block, self.node)
        with_third = self._seed_contract(_new_to(third))
        without = self._seed_contract(_new_to())
        self._call(sender_pk, sender_key, with_third, counter=0)
        tx_hash, receipt = self._call(sender_pk, sender_key, without, counter=1)
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertFalse(self._in_tree(_real_variants(tx_hash, third)[1]))

    def test_block_element_once_per_block(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        third = _seed_third(self.block, self.node)
        contract = self._seed_contract(_new_to(third), balance=1_000_000)
        tx1, r1 = self._call(sender_pk, sender_key, contract, counter=0)
        tx2, r2 = self._call(sender_pk, sender_key, contract, counter=1)
        self.assertEqual(r1.status, STATUS_SUCCESS)
        self.assertEqual(r2.status, STATUS_SUCCESS)
        self.assertEqual(self.inserted.count(_real_variants(tx1, third)[0]), 1)
        self.assertEqual(self.inserted.count(_real_variants(tx1, third)[1]), 1)
        self.assertEqual(self.inserted.count(_real_variants(tx2, third)[1]), 1)

    def test_fee_charged_to_sender(self):
        third = _seed_third(self.block, self.node)
        contract_address = os.urandom(32)
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        self._seed_contract(_new_to(third), address=contract_address)
        _, with_rows = self._call(sender_pk, sender_key, contract_address)

        other = make_block(self.node, self.prev_block)
        self._use_block(other)
        seed_storage_account(other)
        other.accounts.set_account(sender_pk, create_account(balance=10_000_000))
        _seed_third(other, self.node, third)
        other.accounts.set_account(
            contract_address,
            create_account(balance=100_000, code_hash=seed_program(self.node, _new_to(third))),
        )
        with mock.patch(
            "astreum.consensus.transaction.apply.make_involved_variants",
            side_effect=lambda t, a: [],
        ):
            _, without_rows = self._call(sender_pk, sender_key, contract_address)

        expected = calculate_storage_fee(self.block, 2 * 2)
        self.assertEqual(with_rows.storage_fee - without_rows.storage_fee, expected)

    def test_unaffordable_elements_fail_tx(self):
        third = _seed_third(self.block, self.node)
        contract_address = os.urandom(32)
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        self._seed_contract(_new_to(third), address=contract_address)
        _, receipt = self._call(sender_pk, sender_key, contract_address)
        needed = receipt.total_fee

        # The same call from a sender who is 1 short passes the bloom stage and
        # fails at the final check. This is the late failure.
        block = make_block(self.node, self.prev_block)
        self._use_block(block)
        seed_storage_account(block)
        block.accounts.set_account(sender_pk, create_account(balance=needed - 1))
        _seed_third(block, self.node, third)
        block.accounts.set_account(
            contract_address,
            create_account(balance=100_000, code_hash=seed_program(self.node, _new_to(third))),
        )
        tx_hash, receipt = self._call(sender_pk, sender_key, contract_address)
        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(self.block.involved_accounts, set())
        self.assertFalse(self._in_tree(_real_variants(tx_hash, third)[1]))

    def test_loan_owner_is_involved(self):
        owner = os.urandom(32)
        self._use_block(make_block(self.node, self.prev_block, height=20))
        seed_storage_account(self.block)
        self.block.accounts.set_account(owner, create_account(balance=0))
        sender_pk, sender_key = seed_sender_account(self.block, balance=100_000_000)
        loan_tx_id = os.urandom(32)
        loan = TreasuryLoanRecord(
            creation_block_number=0, loan_type=LoanType.SECURED,
            discounted_amount=450, payment_amount=100, payment_interval_blocks=10,
            next_payment_block_number=10, payment_count=5, owner=owner,
        )
        treasury = seed_treasury_account(self.node, self.block, treasury_balance=100_000)
        seed_user_with_loan(
            self.node, treasury, sender=sender_pk, user_balance=10_000,
            loan_tx_id=loan_tx_id, loan=loan,
        )
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=100, code=TransactionCode.TREASURY_REPAY,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)
        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)
        block_element, tx_element = _real_variants(tx_hash, owner)
        self.assertTrue(self._in_tree(block_element))
        self.assertTrue(self._in_tree(tx_element))
        self.assertEqual(self.block.involved_accounts, {owner})


class TestBuilderVerifierAgree(unittest.TestCase):

    def _bloom_hash(self, first, second, third_a, third_b, contract_address, sender_pk, sender_key):
        node = _FakeNode()
        block = make_block(node, make_previous_block())
        seed_storage_account(block)
        block.accounts.set_account(sender_pk, create_account(balance=10_000_000))
        _seed_third(block, node, third_a)
        _seed_third(block, node, third_b)
        block.accounts.set_account(
            contract_address,
            create_account(
                balance=100_000,
                code_hash=seed_program(node, _new_to(first, second)),
            ),
        )
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=contract_address,
            amount=0, code=TransactionCode.CODE_ACCOUNT_CALL,
            data=NIL, secret_key=sender_key, cost_limit=5000,
        )
        apply_transaction(node, block, store_tx(node, tx))
        self.assertEqual(block.receipts[-1].status, STATUS_SUCCESS)
        self.assertEqual(block.involved_accounts, {third_a, third_b})
        return block.bloom_tree.root.expr().hash()

    def test_builder_and_verifier_same_bloom_hash(self):
        a, b = os.urandom(32), os.urandom(32)
        contract_address = os.urandom(32)
        sender_pk, sender_key = seed_sender_account(make_block(_FakeNode(), make_previous_block()))
        first = self._bloom_hash(a, b, a, b, contract_address, sender_pk, sender_key)
        again = self._bloom_hash(a, b, a, b, contract_address, sender_pk, sender_key)
        # The call order does not change the insert order.
        swapped = self._bloom_hash(b, a, a, b, contract_address, sender_pk, sender_key)
        self.assertEqual(first, again)
        self.assertEqual(first, swapped)


if __name__ == "__main__":
    unittest.main()
