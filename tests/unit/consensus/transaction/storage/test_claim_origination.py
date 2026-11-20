"""Claim origination: the amount field elects who pays the claim storage fee.

A claim's storage fee ``C`` is the storage fee the receipt books
(``receipt.storage_fee``). At apply:

- ``amount == 0`` writes a `CLAIM_COST` loan for ``C`` in the sender's
  treasury record loans trie. The storage fee moves no cash; the base fee
  is paid in cash like every transaction's base fee.
- ``amount >= C`` pays the storage fee in full from the amount; the
  remainder transfers to the storage account. No loan is written.
- ``0 < amount < C`` fails the receipt. There are no partial payments.

The base fee never enters a loan and is covered by the sender's balance,
which includes the claim's own credited payout.
"""

import os
import sys
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[5]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
HELPERS_DIR = Path(__file__).resolve().parents[2] / "validation"
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from astreum.consensus.constants import STORAGE_ADDRESS, TREASURY_ADDRESS
from astreum.consensus.block.rate import calculate_quote_rate
from astreum.consensus.block.rate_window import update_statistics
from astreum.consensus.block.reward import fee_income, validator_reward
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction import apply_transaction
from astreum.consensus.transaction import apply as apply_module
from astreum.consensus.transaction.apply import demand_pair
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.storage.model import (
    StorageLane,
    StorageRecord,
    storage_record_from_storage,
    storage_record_to_expr,
)
from astreum.consensus.transaction.treasury.loans import load_user_loan
from astreum.consensus.transaction.treasury.record import (
    LoanType,
    TreasuryUserRecord,
    load_user_record,
)
from astreum.crypto.merkle import create_merkle_tree, generate_merkle_tree_proof
from astreum.expression import Expr, NIL, ZERO32, bytes_, int_, link
from astreum.expression.encoding import encode_expr_to_bytes
from astreum.expression.helpers import bytes_list_to_expr
from astreum.storage.radix import (
    get_from_radix_tree,
    get_radix_node_expr,
    put_in_radix_tree,
)
from astreum.storage.radix.node import radix_node_hash

from _helpers import (
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_sender_account,
    seed_storage_account,
    seed_treasury_account,
    store_expr_tree,
    store_tx,
)
from astreum.consensus.transaction.storage.payment import challenge_index


def _build_claim_expr(claim) -> Expr:
    """Encode one claim tuple as its `Expr` link list."""
    storage_id, slot_bytes, proof, nonce, lane = claim
    return link(
        bytes_(storage_id),
        link(
            bytes_(slot_bytes),
            link(
                bytes_list_to_expr(proof),
                link(int_(nonce), link(int_(lane), NIL)),
            ),
        ),
    )


def _build_claim_payload(claims) -> Expr:
    """Encode a list of claim tuples as the transaction data link list."""
    payload = NIL
    for claim in reversed(claims):
        payload = link(_build_claim_expr(claim), payload)
    return payload


def _slots(n: int = 5) -> list[Expr]:
    """Return *n* random slot expressions."""
    return [bytes_(os.urandom(24)) for _ in range(n)]


def _claim_for(storage_id: bytes, slots: list[Expr], index: int) -> tuple:
    """A claim on slot *index* of *slots*, with its Merkle proof, lane 0."""
    tree = create_merkle_tree([s.hash() for s in slots])
    return (
        storage_id,
        encode_expr_to_bytes(slots[index]),
        generate_merkle_tree_proof(tree, index),
        0,
        0,
    )


def _ceil_fraction(value: Fraction) -> int:
    """Return ``ceil(value)`` for an exact `Fraction`."""
    return -(-value.numerator // value.denominator)


def _claim_service(claim_fee: int, block: object) -> int:
    """``ceil(claim_fee * r_q)`` for *block*, from the plan's formula."""
    return _ceil_fraction(claim_fee * calculate_quote_rate(block))


class TestClaimOrigination(unittest.TestCase):
    """The amount field elects the claim storage fee payer."""

    BASE_FEE = 7

    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self.block = make_block(self.node, self.prev_block)
        seed_storage_account(self.block)

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------

    def _seed_storage_record(
        self,
        storage_id: bytes,
        *,
        winner: bytes,
        new_size: int = 100,
    ) -> tuple[StorageRecord, list[Expr]]:
        """Put a record with one incumbent lane in the storage trie."""
        slots = _slots()
        block_hash = self.block.previous_block_hash
        record = StorageRecord(
            creation_block_hash=block_hash,
            lanes=(
                StorageLane(block_hash=block_hash, height=0, winner=winner),
                StorageLane(block_hash=block_hash, height=0, winner=ZERO32),
            ),
            new_size=new_size,
            new_count=len(slots),
            slots_root=create_merkle_tree([s.hash() for s in slots]).root,
        )
        storage_account = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)
        record_head = store_expr_tree(self.node, storage_record_to_expr(record))
        put_in_radix_tree(storage_account.data, self.node, storage_id, record_head)
        for trie_node in storage_account.data.nodes.values():
            self.node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(
                trie_node
            )
        storage_account.data_hash = storage_account.data.root_hash or ZERO32
        return record, slots

    def _claim(self, record: StorageRecord, slots: list[Expr], storage_id: bytes):
        """A valid claim on the record's incumbent lane."""
        index = challenge_index(
            record.lanes[0].block_hash, storage_id, record.new_count, 0
        )
        return _claim_for(storage_id, slots, index)

    def _build_tx(self, sender_pk: bytes, payload: Expr, amount: int) -> Transaction:
        """Build the claim transaction without applying it."""
        sender = self.block.accounts.get_account(sender_pk, self.node)
        return Transaction(
            chain_id=1,
            amount=amount,
            code=TransactionCode.STORAGE_PAYMENT,
            counter=sender.counter,
            cost_limit=0,
            base_fee=self.BASE_FEE,
            data=payload,
            recipient=STORAGE_ADDRESS,
            sender=sender_pk,
        )

    def _submit_claim(
        self,
        sender_pk: bytes,
        claims,
        *,
        amount: int = 0,
    ) -> bytes:
        """Build, store, and apply one STORAGE_PAYMENT transaction."""
        payload = _build_claim_payload(claims)
        store_expr_tree(self.node, payload)
        tx = self._build_tx(sender_pk, payload, amount)
        tx_hash = store_tx(self.node, tx)
        apply_transaction(self.node, self.block, tx_hash)
        return tx_hash

    def _seed_record_and_claim(self, sender: bytes, *, new_size: int = 100):
        """Seed a storage record and return ``(storage_id, claim)``."""
        storage_id = os.urandom(32)
        record, slots = self._seed_storage_record(
            storage_id, winner=sender, new_size=new_size
        )
        return storage_id, self._claim(record, slots, storage_id)

    def _loan(self, sender: bytes, tx_hash: bytes):
        """Load one loan after flushing the block's pending expressions."""
        flush_pending(self.node, self.block)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        return load_user_loan(
            node=self.node,
            treasury_account=treasury,
            borrower=sender,
            loan_transaction_id=tx_hash,
        )

    def _balances(self, sender: bytes) -> tuple[int, int, int]:
        """Return (sender balance, treasury balance, storage balance)."""
        sender_account = self.block.accounts.get_account(sender, self.node)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        storage = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)
        return (
            sender_account.balance,
            treasury.balance if treasury is not None else 0,
            storage.balance,
        )

    def _record(self, borrower: bytes):
        """Load the borrower's treasury user record, if any."""
        flush_pending(self.node, self.block)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        if treasury is None:
            return None
        return load_user_record(self.node, treasury.data, borrower)

    def _assert_fee_booked(self, receipt) -> None:
        """Pin the receipt's concrete fee fields and the fee income."""
        self.assertEqual(receipt.base_fee, self.BASE_FEE)
        self.assertEqual(receipt.data_fee, 0)
        self.assertEqual(receipt.execution_fee, 0)
        self.assertGreater(receipt.storage_fee, 0)
        booked = (
            receipt.base_fee
            + receipt.storage_fee
            + receipt.data_fee
            + receipt.execution_fee
        )
        self.assertEqual(booked, receipt.total_fee)
        self.assertEqual(fee_income(self.block.receipts), booked)
        before = self.prev_block.statistics
        after = update_statistics(
            before,
            self.block.total_balance,
            fee_income(self.block.receipts),
            self.block.storage_claim_count,
            *demand_pair(self.block),
        )
        self.assertEqual(after[0][0] - before[0][0], booked)

    # ------------------------------------------------------------------
    # amount == 0: the loan finances the storage fee
    # ------------------------------------------------------------------

    def test_zero_amount_claim_finances_the_storage_fee_only(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        seed_treasury_account(
            self.node,
            self.block,
            treasury_balance=50_000,
            user_records={sender: TreasuryUserRecord(balance=10_000)},
        )
        storage_id, claim = self._seed_record_and_claim(sender)

        sender_before, treasury_before, storage_before = self._balances(sender)
        tx_hash = self._submit_claim(sender, [claim])

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertGreater(receipt.storage_fee, receipt.base_fee)

        loan = self._loan(sender, tx_hash)
        self.assertIsNotNone(loan)
        self.assertEqual(loan.loan_type, LoanType.CLAIM_COST)
        # The principal is the storage fee only: the base fee is never
        # part of a claim loan.
        self.assertEqual(loan.discounted_amount, receipt.storage_fee)
        self.assertEqual(
            loan.payment_amount, _claim_service(receipt.storage_fee, self.block)
        )
        self.assertEqual(loan.paid_until_block, self.block.height)
        self.assertEqual(loan.creation_block_number, self.block.height)
        self.assertEqual(loan.owner, TREASURY_ADDRESS)
        self.assertEqual(loan.missed_count, 0)

        # The storage fee moves no cash. The only cash is the base fee.
        sender_after, treasury_after, storage_after = self._balances(sender)
        self.assertEqual(
            sender_after, sender_before + receipt.mint - receipt.base_fee
        )
        self.assertEqual(treasury_after, treasury_before)
        self.assertEqual(storage_after, storage_before)

        user_record = self._record(sender)
        self.assertEqual(user_record.balance, 10_000)

        self._assert_fee_booked(receipt)

    def test_financed_claim_reward_is_the_cash_base_fee(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        storage_id, claim = self._seed_record_and_claim(sender)

        tx_hash = self._submit_claim(sender, [claim])

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        # total_fee - storage_fee is the base fee, and the sender paid it in
        # cash: no phantom mint from the financed storage fee.
        self.assertEqual(validator_reward(self.block.receipts), receipt.base_fee)
        loan = self._loan(sender, tx_hash)
        self.assertEqual(loan.discounted_amount, receipt.storage_fee)

    def test_zero_balance_claim_with_payout_below_the_storage_fee_succeeds(self):
        # The payout covers the base fee but not the storage fee. The storage
        # fee is financed; the old pre-credit made the payout pay all of it.
        sender, _ = seed_sender_account(self.block, balance=0)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        storage_id, claim = self._seed_record_and_claim(sender, new_size=10)

        tx_hash = self._submit_claim(sender, [claim])

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertLessEqual(receipt.base_fee, receipt.mint)
        self.assertGreater(receipt.storage_fee, receipt.mint)

        sender_after, treasury_after, storage_after = self._balances(sender)
        self.assertEqual(sender_after, receipt.mint - receipt.base_fee)
        self.assertEqual(treasury_after, 0)
        self.assertEqual(storage_after, 0)

        loan = self._loan(sender, tx_hash)
        self.assertIsNotNone(loan)
        self.assertEqual(loan.discounted_amount, receipt.storage_fee)
        self._assert_fee_booked(receipt)

    def test_zero_balance_claim_below_the_base_fee_fails(self):
        # The base fee is cash and is never financed.
        sender, _ = seed_sender_account(self.block, balance=0)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        storage_id, claim = self._seed_record_and_claim(sender, new_size=1)

        tx_hash = self._submit_claim(sender, [claim])

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertIsNone(self._loan(sender, tx_hash))
        sender_after, _, _ = self._balances(sender)
        self.assertEqual(sender_after, 0)

    def test_first_time_claimant_with_no_treasury_record_gets_one(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        storage_id, claim = self._seed_record_and_claim(sender)

        tx_hash = self._submit_claim(sender, [claim])

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user_record = self._record(sender)
        self.assertIsNotNone(user_record)
        self.assertEqual(user_record.balance, 0)
        self.assertNotEqual(user_record.loans_root_hash, ZERO32)
        self.assertEqual(
            self._loan(sender, tx_hash).discounted_amount,
            receipt.storage_fee,
        )
        self._assert_fee_booked(receipt)

    # ------------------------------------------------------------------
    # amount >= C: the amount pays the storage fee, the remainder transfers
    # ------------------------------------------------------------------

    def test_amount_at_least_the_storage_fee_pays_it_and_writes_no_loan(self):
        sender, _ = seed_sender_account(self.block, balance=10**12)
        treasury = seed_treasury_account(
            self.node,
            self.block,
            treasury_balance=50_000,
            user_records={sender: TreasuryUserRecord(balance=10_000)},
        )
        loans_root_before = treasury.data.root_hash
        storage_id, claim = self._seed_record_and_claim(sender)

        amount = 10**9
        sender_before, treasury_before, storage_before = self._balances(sender)
        tx_hash = self._submit_claim(sender, [claim], amount=amount)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        claim_fee = receipt.storage_fee
        self.assertGreater(claim_fee, 0)
        self.assertGreaterEqual(amount, claim_fee)

        # The amount pays C and the remainder; the base fee is extra cash.
        sender_after, treasury_after, storage_after = self._balances(sender)
        self.assertEqual(
            sender_after,
            sender_before + receipt.mint - receipt.base_fee - amount,
        )
        self.assertEqual(treasury_after, treasury_before)
        self.assertEqual(storage_after, storage_before + amount)

        # No loan: the treasury record is untouched.
        flush_pending(self.node, self.block)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        self.assertEqual(treasury.data.root_hash, loans_root_before)
        self.assertIsNone(self._loan(sender, tx_hash))

        self._assert_fee_booked(receipt)

    def test_amount_funded_by_the_claim_payout_writes_no_loan(self):
        # A zero-balance provider can choose amount >= C and pay it from the
        # payout the handler credits. No loan is written.
        sender, _ = seed_sender_account(self.block, balance=0)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        storage_id, claim = self._seed_record_and_claim(sender, new_size=20_000)

        amount = 15_000
        tx_hash = self._submit_claim(sender, [claim], amount=amount)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertGreaterEqual(amount, receipt.storage_fee)
        self.assertEqual(receipt.mint, 20_000)

        sender_after, treasury_after, storage_after = self._balances(sender)
        self.assertEqual(
            sender_after, receipt.mint - receipt.base_fee - amount
        )
        self.assertEqual(treasury_after, 0)
        self.assertEqual(storage_after, amount)
        self.assertIsNone(self._loan(sender, tx_hash))
        self._assert_fee_booked(receipt)

    def _apply_with_storage_fee(self, sender: bytes, claim, amount: int, storage_fee: int):
        """Apply a claim with a pinned receipt storage fee."""
        payload = _build_claim_payload([claim])
        store_expr_tree(self.node, payload)
        tx = self._build_tx(sender, payload, amount)
        tx_hash = store_tx(self.node, tx)

        def fixed_point(node, block, build, base_storage_fee, named):
            """Build a receipt whose storage fee is *storage_fee*."""
            return build(storage_fee)

        with patch.object(
            apply_module, "receipt_at_fixed_point", side_effect=fixed_point
        ):
            apply_transaction(self.node, self.block, tx_hash)
        return tx_hash

    def test_amount_equal_to_the_storage_fee_is_accepted(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        storage_id, claim = self._seed_record_and_claim(sender)

        storage_fee = 1_000
        sender_before, treasury_before, storage_before = self._balances(sender)
        tx_hash = self._apply_with_storage_fee(
            sender, claim, amount=storage_fee, storage_fee=storage_fee
        )

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(receipt.storage_fee, storage_fee)
        # The cash path books the base fee normally: no loan.
        self.assertEqual(receipt.base_fee, self.BASE_FEE)
        sender_after, treasury_after, storage_after = self._balances(sender)
        self.assertEqual(
            sender_after,
            sender_before + receipt.mint - receipt.base_fee - storage_fee,
        )
        self.assertEqual(treasury_after, treasury_before)
        self.assertEqual(storage_after, storage_before + storage_fee)
        self.assertIsNone(self._loan(sender, tx_hash))

    # ------------------------------------------------------------------
    # 0 < amount < C: the receipt fails
    # ------------------------------------------------------------------

    def test_partial_amount_below_the_storage_fee_fails_the_receipt(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        seed_treasury_account(
            self.node,
            self.block,
            treasury_balance=50_000,
            user_records={sender: TreasuryUserRecord(balance=10_000)},
        )
        storage_id, claim = self._seed_record_and_claim(sender)

        sender_before, treasury_before, storage_before = self._balances(sender)
        tx_hash = self._submit_claim(sender, [claim], amount=1)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertGreater(receipt.storage_fee, 1)

        # No loan and no claim: the receipt failed, the handler writes are
        # rolled back, and no amount transfers.
        self.assertIsNone(self._loan(sender, tx_hash))
        flush_pending(self.node, self.block)
        storage_account = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)
        head = get_from_radix_tree(storage_account.data, self.node, storage_id)
        self.assertIsNotNone(head)
        stored = storage_record_from_storage(self.node, head.hash())
        self.assertEqual(stored.lanes[0].height, 0)
        self.assertEqual(stored.lanes[0].winner, sender)

        # No partial treasury write.
        user_record = self._record(sender)
        self.assertIsNotNone(user_record)
        self.assertEqual(user_record.balance, 10_000)
        self.assertEqual(user_record.loans_root_hash, ZERO32)

        # The failed claim still pays its fees, as every failed tx does.
        sender_after, treasury_after, storage_after = self._balances(sender)
        self.assertEqual(sender_after, sender_before - receipt.total_fee)
        self.assertEqual(treasury_after, treasury_before)
        self.assertEqual(storage_after, storage_before + receipt.storage_fee)

    def test_a_second_zero_amount_claim_keeps_the_first_loan(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        first_id, first_claim = self._seed_record_and_claim(sender)
        second_id, second_claim = self._seed_record_and_claim(sender)

        first_hash = self._submit_claim(sender, [first_claim])
        second_hash = self._submit_claim(sender, [second_claim])

        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)
        self.assertIsNotNone(self._loan(sender, first_hash))
        self.assertIsNotNone(self._loan(sender, second_hash))
        self.assertNotEqual(first_hash, second_hash)

    # ------------------------------------------------------------------
    # the plan's rate identity
    # ------------------------------------------------------------------

    def test_claim_service_is_byte_rent_within_rounding(self):
        # Computed from the spec and plan formulas, not from the code:
        # C = 2 * ceil(bytes * fee_rate / r_q), service = ceil(C * r_q),
        # and the service is 2 * bytes * fee_rate up to rounding
        # (spec 192-193, plan 132-134).
        stored_bytes = 1318
        fee_rate = Fraction(1, 3)
        quote = Fraction(1, 7)

        claim_fee = 2 * _ceil_fraction(Fraction(stored_bytes) * fee_rate / quote)
        service = _ceil_fraction(claim_fee * quote)
        byte_rent = 2 * stored_bytes * fee_rate

        self.assertEqual(claim_fee, 6152)
        self.assertEqual(service, 879)
        self.assertGreaterEqual(service, byte_rent)
        self.assertLess(service - byte_rent, 1 + 2 * quote)


if __name__ == "__main__":
    unittest.main()
