"""Claim settlement nets the payout against every loan of the sender.

The rules under test (Plan B, Task 3):

- A claim paying rent ``R`` with accrued service ``S`` delivers ``R - S`` to
  the sender and pays each loan's ``owner`` its share of ``S``.
- Every settled loan's ``paid_until_block`` advances to the current height
  ``h``.
- When ``R < S`` the sender keeps the whole rent and no owner is paid:
  nothing is settled, so every pointer stays where it was and the accrued
  service stays owed on the loans.
- A second financed claim from the same account settles the first loan's
  accrued service at once, so debt cannot stack.
- The claim's fee is not part of the netting.
- A sold loan pays its buyer, not the treasury.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
HELPERS_DIR = Path(__file__).resolve().parents[2] / "validation"
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from astreum.consensus.constants import STORAGE_ADDRESS, TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction import apply_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.storage.model import (
    StorageLane,
    StorageRecord,
    storage_record_from_storage,
    storage_record_to_expr,
)
from astreum.consensus.transaction.treasury.loans import (
    load_user_loan,
    load_user_loans,
)
from astreum.consensus.transaction.treasury.record import (
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
    load_user_record,
)
from astreum.crypto.merkle import create_merkle_tree, generate_merkle_tree_proof
from astreum.expression import Expr, NIL, ZERO32, bytes_, int_, link
from astreum.expression.encoding import encode_expr_to_bytes
from astreum.expression.helpers import bytes_list_to_expr
from astreum.storage.radix import (
    RadixTree,
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
    seed_account,
    seed_sender_account,
    seed_storage_account,
    seed_treasury_account,
    store_expr_tree,
    store_tx,
)
from astreum.consensus.transaction.storage.payment import challenge_index


BASE_FEE = 7
HEIGHT = 10


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


class TestClaimNetting(unittest.TestCase):
    """Settlement collects every loan's service from the claim's payout."""

    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self.block = make_block(self.node, self.prev_block, height=HEIGHT)
        seed_storage_account(self.block)

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------

    def _seed_storage_record(
        self,
        storage_id: bytes,
        *,
        winner: bytes,
        new_size: int = 1000,
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
        storage_account = self.block.accounts.get_account(
            STORAGE_ADDRESS, self.node
        )
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

    def _seed_record_and_claim(self, sender: bytes, *, new_size: int = 1000):
        """Seed a storage record and return ``(storage_id, claim)``."""
        storage_id = os.urandom(32)
        record, slots = self._seed_storage_record(
            storage_id, winner=sender, new_size=new_size
        )
        return storage_id, self._claim(record, slots, storage_id)

    def _build_tx(self, sender_pk: bytes, payload: Expr, amount: int) -> Transaction:
        """Build the claim transaction without applying it."""
        sender = self.block.accounts.get_account(sender_pk, self.node)
        return Transaction(
            chain_id=1,
            amount=amount,
            code=TransactionCode.STORAGE_PAYMENT,
            counter=sender.counter,
            cost_limit=0,
            base_fee=BASE_FEE,
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

    def _treasury(self):
        """The treasury account, after flushing the block's pending exprs."""
        flush_pending(self.node, self.block)
        return self.block.accounts.get_account(TREASURY_ADDRESS, self.node)

    def _seed_loans(
        self,
        sender: bytes,
        loans: list[tuple[bytes, TreasuryLoanRecord]],
        *,
        user_balance: int = 0,
    ) -> TreasuryUserRecord:
        """Attach several pre-existing loans to one treasury user record."""
        loans_trie = RadixTree()
        for loan_transaction_id, loan in loans:
            loan_head = store_expr_tree(self.node, loan.expr())
            put_in_radix_tree(
                loans_trie, self.node, loan_transaction_id, loan_head
            )
        for trie_node in loans_trie.nodes.values():
            self.node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(
                trie_node
            )

        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        user_record = TreasuryUserRecord(
            balance=user_balance,
            loans_root_hash=loans_trie.root_hash or ZERO32,
            total_interest_paid=0,
        )
        rec_head = store_expr_tree(self.node, user_record.expr())
        put_in_radix_tree(treasury.data, self.node, sender, rec_head)
        for trie_node in treasury.data.nodes.values():
            self.node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(
                trie_node
            )
        treasury.data_hash = treasury.data.root_hash or ZERO32
        return user_record

    def _loan(self, sender: bytes, loan_transaction_id: bytes):
        """Load one loan after flushing the block's pending expressions."""
        flush_pending(self.node, self.block)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        return load_user_loan(
            node=self.node,
            treasury_account=treasury,
            borrower=sender,
            loan_transaction_id=loan_transaction_id,
        )

    def _loans(self, sender: bytes):
        """Load every loan of *sender* after flushing pending expressions."""
        flush_pending(self.node, self.block)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        return dict(
            load_user_loans(
                node=self.node,
                treasury_account=treasury,
                borrower=sender,
            )
        )

    def _balance(self, address: bytes) -> int:
        """Return one account's balance."""
        account = self.block.accounts.get_account(address, self.node)
        return account.balance if account is not None else 0

    def _loan_record(
        self,
        *,
        payment_amount: int,
        paid_until_block: int,
        owner: bytes = TREASURY_ADDRESS,
        creation_block_number: int = 1,
    ) -> TreasuryLoanRecord:
        """A `CLAIM_COST` loan in the state a past claim would leave it in."""
        return TreasuryLoanRecord(
            creation_block_number=creation_block_number,
            loan_type=LoanType.CLAIM_COST,
            discounted_amount=5_000,
            payment_amount=payment_amount,
            paid_until_block=paid_until_block,
            owner=owner,
        )

    # ------------------------------------------------------------------
    # R >= S: full settlement
    # ------------------------------------------------------------------

    def test_full_settlement_pays_the_owner_and_advances_the_pointer(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        loan_id = os.urandom(32)
        # 6 overdue blocks at 30 per block: service S = 180.
        self._seed_loans(
            sender,
            [(loan_id, self._loan_record(payment_amount=30, paid_until_block=4))],
        )
        _, claim = self._seed_record_and_claim(sender, new_size=1000)
        # Rent R = new_size * (height - lane.height) = 1000 * 10.
        rent = 1000 * HEIGHT
        service = (HEIGHT - 4) * 30

        sender_before = self._balance(sender)
        tx_hash = self._submit_claim(sender, [claim])

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(receipt.mint, rent)

        # The sender keeps the rent minus the service, and pays the base fee.
        self.assertEqual(
            self._balance(sender),
            sender_before + rent - service - receipt.base_fee,
        )
        # The owner receives the deducted service.
        self.assertEqual(self._balance(TREASURY_ADDRESS), service)
        # The fee is not part of the netting: the storage fee moved no cash.
        self.assertGreater(receipt.storage_fee, 0)
        self.assertEqual(self._balance(STORAGE_ADDRESS), 0)

        loan = self._loan(sender, loan_id)
        self.assertEqual(loan.paid_until_block, HEIGHT)

        user_record = load_user_record(
            self.node,
            self.block.accounts.get_account(TREASURY_ADDRESS, self.node).data,
            sender,
        )
        self.assertEqual(user_record.total_interest_paid, service)
        # The financed claim also originates its own loan.
        self.assertIsNotNone(self._loan(sender, tx_hash))

    def test_two_loans_with_different_owners_each_pay_their_owner(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        buyer = os.urandom(32)
        seed_account(self.block, buyer, balance=0)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        treasury_loan_id = os.urandom(32)
        buyer_loan_id = os.urandom(32)
        self._seed_loans(
            sender,
            [
                (
                    treasury_loan_id,
                    self._loan_record(payment_amount=30, paid_until_block=4),
                ),
                (
                    buyer_loan_id,
                    self._loan_record(
                        payment_amount=50,
                        paid_until_block=6,
                        owner=buyer,
                    ),
                ),
            ],
        )
        _, claim = self._seed_record_and_claim(sender, new_size=1000)
        rent = 1000 * HEIGHT
        treasury_service = (HEIGHT - 4) * 30
        buyer_service = (HEIGHT - 6) * 50

        sender_before = self._balance(sender)
        self._submit_claim(sender, [claim])

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(
            self._balance(sender),
            sender_before
            + rent
            - treasury_service
            - buyer_service
            - receipt.base_fee,
        )
        self.assertEqual(self._balance(TREASURY_ADDRESS), treasury_service)
        self.assertEqual(self._balance(buyer), buyer_service)
        self.assertEqual(
            self._loan(sender, treasury_loan_id).paid_until_block, HEIGHT
        )
        self.assertEqual(
            self._loan(sender, buyer_loan_id).paid_until_block, HEIGHT
        )

    def test_a_prepaid_loan_keeps_its_pointer(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        loan_id = os.urandom(32)
        self._seed_loans(
            sender,
            [(loan_id, self._loan_record(payment_amount=30, paid_until_block=15))],
        )
        _, claim = self._seed_record_and_claim(sender, new_size=1000)
        rent = 1000 * HEIGHT

        sender_before = self._balance(sender)
        self._submit_claim(sender, [claim])

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(
            self._balance(sender), sender_before + rent - receipt.base_fee
        )
        self.assertEqual(self._balance(TREASURY_ADDRESS), 0)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, 15)

    def test_a_closed_loan_is_not_settled(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        loan_id = os.urandom(32)
        # A closed loan carries paid_until_block == 0; its pointer is not an
        # arrears clock, so the claim pays the whole rent.
        self._seed_loans(
            sender,
            [(loan_id, self._loan_record(payment_amount=30, paid_until_block=0))],
        )
        _, claim = self._seed_record_and_claim(sender, new_size=1000)
        rent = 1000 * HEIGHT

        sender_before = self._balance(sender)
        self._submit_claim(sender, [claim])

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(
            self._balance(sender), sender_before + rent - receipt.base_fee
        )
        self.assertEqual(self._balance(TREASURY_ADDRESS), 0)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, 0)

    # ------------------------------------------------------------------
    # R < S: the provider keeps the whole rent and the arrears stay
    # ------------------------------------------------------------------

    def test_a_short_payout_pays_the_provider_whole_and_keeps_the_arrears(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        buyer = os.urandom(32)
        seed_account(self.block, buyer, balance=0)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        loan_id = os.urandom(32)
        buyer_loan_id = os.urandom(32)
        # Rent R = 10 * 10 = 100; service S = 180 + 25 = 205, so R < S.
        self._seed_loans(
            sender,
            [
                (
                    loan_id,
                    self._loan_record(payment_amount=20, paid_until_block=1),
                ),
                (
                    buyer_loan_id,
                    self._loan_record(
                        payment_amount=5,
                        paid_until_block=5,
                        owner=buyer,
                    ),
                ),
            ],
        )
        _, claim = self._seed_record_and_claim(sender, new_size=10)
        rent = 10 * HEIGHT
        service = (HEIGHT - 1) * 20 + (HEIGHT - 5) * 5
        self.assertLess(rent, service)

        sender_before = self._balance(sender)
        tx_hash = self._submit_claim(sender, [claim])

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(receipt.mint, rent)
        # The rent is the provider's own money: it keeps all of it and pays
        # only the cash base fee.
        self.assertEqual(
            self._balance(sender), sender_before + rent - receipt.base_fee
        )
        # No settlement: neither owner receives anything, and the accrued
        # service stays exactly as it is on the loans.
        self.assertEqual(self._balance(TREASURY_ADDRESS), 0)
        self.assertEqual(self._balance(buyer), 0)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, 1)
        self.assertEqual(
            self._loan(sender, buyer_loan_id).paid_until_block, 5
        )
        # The election is untouched by netting: the zero-amount claim still
        # finances its own storage fee as a loan, and moves no cash for it.
        fee_loan = self._loan(sender, tx_hash)
        self.assertIsNotNone(fee_loan)
        self.assertEqual(fee_loan.discounted_amount, receipt.storage_fee)
        self.assertEqual(fee_loan.paid_until_block, HEIGHT)
        self.assertEqual(self._balance(STORAGE_ADDRESS), 0)
        self.assertEqual(len(self._loans(sender)), 3)

    def test_rent_equal_to_the_service_settles_with_a_zero_sender_payout(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        loan_id = os.urandom(32)
        # Rent R = 18 * 10 = 180; service S = 9 * 20 = 180, so R == S.
        self._seed_loans(
            sender,
            [(loan_id, self._loan_record(payment_amount=20, paid_until_block=1))],
        )
        _, claim = self._seed_record_and_claim(sender, new_size=18)
        rent = 18 * HEIGHT
        service = (HEIGHT - 1) * 20
        self.assertEqual(rent, service)

        sender_before = self._balance(sender)
        self._submit_claim(sender, [claim])

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(receipt.mint, rent)
        # The sender keeps nothing but still pays the cash base fee.
        self.assertEqual(self._balance(sender), sender_before - receipt.base_fee)
        # The whole rent reaches the owner and the pointer advances.
        self.assertEqual(self._balance(TREASURY_ADDRESS), service)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, HEIGHT)

    def test_every_claim_in_a_multi_claim_transaction_pays(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        loan_id = os.urandom(32)
        # One loan, service S = 9 * 20 = 180.
        self._seed_loans(
            sender,
            [(loan_id, self._loan_record(payment_amount=20, paid_until_block=1))],
        )
        # The first claim's rent is 100 < 180: it pays the sender whole and
        # settles nothing. The second claim's rent is 10 000 and settles the
        # same accrued service.
        short_id, short_claim = self._seed_record_and_claim(sender, new_size=10)
        paying_id, paying_claim = self._seed_record_and_claim(
            sender, new_size=1000
        )
        short_rent = 10 * HEIGHT
        paying_rent = 1000 * HEIGHT
        service = (HEIGHT - 1) * 20

        sender_before = self._balance(sender)
        tx_hash = self._submit_claim(sender, [short_claim, paying_claim])

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        # Both claims pay: the short one whole, the second one net of service.
        self.assertEqual(receipt.mint, short_rent + paying_rent)
        self.assertEqual(
            self._balance(sender),
            sender_before
            + short_rent
            + paying_rent
            - service
            - receipt.base_fee,
        )
        self.assertEqual(self._balance(TREASURY_ADDRESS), service)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, HEIGHT)

        # Both records are claimed: no claim is skipped.
        flush_pending(self.node, self.block)
        storage_account = self.block.accounts.get_account(
            STORAGE_ADDRESS, self.node
        )
        for storage_id in (short_id, paying_id):
            head = get_from_radix_tree(
                storage_account.data, self.node, storage_id
            )
            record = storage_record_from_storage(self.node, head.hash())
            self.assertEqual(record.lanes[0].height, HEIGHT)
            self.assertEqual(record.lanes[0].winner, sender)

        # The transaction still finances its own claim fee as one loan.
        self.assertIsNotNone(self._loan(sender, tx_hash))
        self.assertEqual(len(self._loans(sender)), 2)

    # ------------------------------------------------------------------
    # the second financed claim settles the first one
    # ------------------------------------------------------------------

    def test_a_second_financed_claim_settles_the_first_loan_at_once(self):
        first = make_block(self.node, self.prev_block, height=1)
        seed_storage_account(first)
        sender, _ = seed_sender_account(first, balance=1_000_000)
        seed_treasury_account(self.node, first, treasury_balance=0)

        self.block = first
        _, claim = self._seed_record_and_claim(sender, new_size=10_000)
        first_tx_hash = self._submit_claim(sender, [claim])
        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)
        first_loan = self._loan(sender, first_tx_hash)
        self.assertIsNotNone(first_loan)
        self.assertEqual(first_loan.paid_until_block, 1)
        service_per_block = first_loan.payment_amount
        flush_pending(self.node, first)

        second = make_block(self.node, first, height=HEIGHT)
        second.accounts = first.accounts
        second.total_balance = first.total_balance
        # The block's own statistics are written by the worker; the fee
        # calculation only reads the parent's, which the genesis block holds.
        first.statistics = self.prev_block.statistics
        self.block = second
        sender_before = self._balance(sender)
        second_tx_hash = self._submit_claim(sender, [claim])

        receipt = second.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        rent = 10_000 * (HEIGHT - 1)
        service = (HEIGHT - 1) * service_per_block
        self.assertGreaterEqual(rent, service)
        self.assertEqual(receipt.mint, rent)
        # The sender keeps the rent minus the first loan's service; a bug
        # that credited the full rent would pass every assertion above.
        self.assertEqual(
            self._balance(sender),
            sender_before + rent - service - receipt.base_fee,
        )

        # The first loan is settled at once; the second starts at height h.
        settled = self._loan(sender, first_tx_hash)
        self.assertEqual(settled.paid_until_block, HEIGHT)
        new_loan = self._loan(sender, second_tx_hash)
        self.assertIsNotNone(new_loan)
        self.assertEqual(new_loan.paid_until_block, HEIGHT)
        self.assertEqual(self._balance(TREASURY_ADDRESS), service)
        self.assertEqual(len(self._loans(sender)), 2)

    # ------------------------------------------------------------------
    # sold loans and the fee
    # ------------------------------------------------------------------

    def test_a_sold_loan_pays_its_buyer_not_the_treasury(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        buyer = os.urandom(32)
        seed_account(self.block, buyer, balance=0)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        loan_id = os.urandom(32)
        self._seed_loans(
            sender,
            [
                (
                    loan_id,
                    self._loan_record(
                        payment_amount=40,
                        paid_until_block=5,
                        owner=buyer,
                    ),
                )
            ],
        )
        _, claim = self._seed_record_and_claim(sender, new_size=1000)
        service = (HEIGHT - 5) * 40

        self._submit_claim(sender, [claim])

        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)
        self.assertEqual(self._balance(buyer), service)
        self.assertEqual(self._balance(TREASURY_ADDRESS), 0)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, HEIGHT)

    def test_the_storage_fee_is_not_deducted_from_the_payout(self):
        sender, _ = seed_sender_account(self.block, balance=10**12)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        loan_id = os.urandom(32)
        self._seed_loans(
            sender,
            [(loan_id, self._loan_record(payment_amount=30, paid_until_block=4))],
        )
        _, claim = self._seed_record_and_claim(sender, new_size=1000)
        rent = 1000 * HEIGHT
        service = (HEIGHT - 4) * 30
        amount = 10**9

        sender_before = self._balance(sender)
        storage_before = self._balance(STORAGE_ADDRESS)
        self._submit_claim(sender, [claim], amount=amount)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertGreater(receipt.storage_fee, 0)
        # The amount pays the fee and transfers its remainder: the netting
        # subtracts only the service, never the storage fee.
        self.assertEqual(
            self._balance(sender),
            sender_before
            + rent
            - service
            - receipt.base_fee
            - amount,
        )
        self.assertEqual(
            self._balance(STORAGE_ADDRESS), storage_before + amount
        )
        self.assertEqual(self._balance(TREASURY_ADDRESS), service)

    def test_a_claim_with_no_loans_pays_the_whole_rent(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        seed_treasury_account(self.node, self.block, treasury_balance=0)
        _, claim = self._seed_record_and_claim(sender, new_size=1000)
        rent = 1000 * HEIGHT

        sender_before = self._balance(sender)
        self._submit_claim(sender, [claim])

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(receipt.mint, rent)
        self.assertEqual(
            self._balance(sender), sender_before + rent - receipt.base_fee
        )
        self.assertEqual(self._balance(TREASURY_ADDRESS), 0)


if __name__ == "__main__":
    unittest.main()
