"""Validation tests for the TREASURY_BUY (0x25) transaction code.

A buyout transfers a loan's ownership for its all-time price. The plan's
formula for an open loan at the current all-time rate ``r(h) = fee / stake`` is

    price = (payment_amount * stake + fee - 1) // fee

where ``fee`` and ``stake`` are the previous block's ``cumulative_total_fee``
and ``cumulative_total_balance``. The buyer pays the price and the
``tx.amount - price`` excess never leaves the buyer's account, so the buyer
needs only ``price`` in balance. The price goes to the current owner (or the
treasury when it owns the loan). The pointer does not move.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
HELPERS_DIR = Path(__file__).resolve().parent
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from astreum.consensus.account import create_account
from astreum.consensus.transaction import apply_transaction, create_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.treasury.record import (
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
)
from astreum.consensus.transaction.treasury.buy import _buyout_price, handle_treasury_buy
from astreum.expression import NIL, bytes_, link
from astreum.storage.radix import (
    RadixTree,
    get_from_radix_tree,
)
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS

from _helpers import (
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_sender_account,
    seed_storage_account,
    seed_treasury_account,
    seed_user_with_loan,
    store_tx,
)

PAYMENT = 100
PRINCIPAL = 450
TREASURY_BALANCE = 100_000


def _buy_data(loan_tx_id):
    """Encode a buy's data: a one-element link-list holding the loan id."""
    return link(bytes_(loan_tx_id), NIL)


class TestTreasuryBuy(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()

    def _make_block(self, height, *, fee=1000, stake=100_000):
        prev_block = make_previous_block(
            cumulative_total_balance=stake, cumulative_fee=fee,
            )
        block = make_block(self.node, prev_block, height=height)
        seed_storage_account(block)
        return block

    def _price(self, payment_amount=PAYMENT, *, fee=1000, stake=100_000):
        """The plan's all-time price: ceil(payment_amount * stake / fee)."""
        return (payment_amount * stake + fee - 1) // fee

    def _seed_loan_tx(self, borrower_pk):
        """Store a real borrow transaction; the loan is keyed by its hash.

        ``TREASURY_BUY`` finds the borrower through the loan's originating
        transaction, so the trie key must be a stored transaction hash.
        """
        loan_tx = create_transaction(
            chain_id=1, counter=0, sender=borrower_pk,
            recipient=TREASURY_ADDRESS, amount=PRINCIPAL,
            code=TransactionCode.TREASURY_BORROW,
        )
        return store_tx(self.node, loan_tx)

    def _seed_loan(
        self,
        block,
        sender_pk,
        *,
        paid_until=5,
        creation=0,
        loan_type=LoanType.SECURED,
        payment_amount=PAYMENT,
        principal=PRINCIPAL,
        owner=TREASURY_ADDRESS,
        user_balance=10_000,
        treasury_balance=TREASURY_BALANCE,
    ):
        loan_tx_id = self._seed_loan_tx(sender_pk)
        loan = TreasuryLoanRecord(
            creation_block_number=creation,
            loan_type=loan_type,
            discounted_amount=principal,
            payment_amount=payment_amount,
            paid_until_block=paid_until,
            owner=owner,
        )
        treasury = seed_treasury_account(
            self.node, block, treasury_balance=treasury_balance,
        )
        seed_user_with_loan(
            self.node, treasury,
            sender=sender_pk, user_balance=user_balance,
            loan_tx_id=loan_tx_id, loan=loan,
        )
        return treasury, loan_tx_id, loan

    def _get_loan(self, block, sender_pk, loan_tx_id):
        treasury = block.accounts.get_account(TREASURY_ADDRESS, self.node)
        user_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        user = TreasuryUserRecord.from_storage(self.node, user_head)
        loans_trie = RadixTree(root_hash=bytes(user.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, loan_tx_id)
        return TreasuryLoanRecord.from_storage(self.node, loan_head)

    def _buy(self, block, sender_pk, sender_key, loan_tx_id, amount, *, counter=0,
             recipient=TREASURY_ADDRESS):
        tx = create_transaction(
            chain_id=1, counter=counter, sender=sender_pk, recipient=recipient,
            amount=amount, code=TransactionCode.TREASURY_BUY,
            data=_buy_data(loan_tx_id), secret_key=sender_key,
        )
        apply_transaction(self.node, block, store_tx(self.node, tx))
        flush_pending(self.node, block)
        return block.receipts[-1]

    def _apply_buy_handler_only(self, block, buyer_pk, loan_tx_id, amount):
        """Call the handler directly, without ``apply``'s snapshot rollback."""
        tx = create_transaction(
            chain_id=1, counter=0, sender=buyer_pk, recipient=TREASURY_ADDRESS,
            amount=amount, code=TransactionCode.TREASURY_BUY,
            data=_buy_data(loan_tx_id),
        )
        return handle_treasury_buy(
            node=self.node, block=block, transaction=tx,
            transaction_hash=os.urandom(32),
        )

    # --- price and payment ---

    def test_buy_pays_the_all_time_price_and_transfers_ownership(self):
        block = self._make_block(height=5)
        buyer_pk, buyer_key = seed_sender_account(block, balance=1_000_000)
        owner = os.urandom(32)
        block.accounts.set_account(owner, create_account(balance=7))
        _, loan_tx_id, _ = self._seed_loan(block, buyer_pk, owner=owner)
        price = self._price()
        buyer_before = block.accounts.get_account(buyer_pk, self.node).balance

        receipt = self._buy(block, buyer_pk, buyer_key, loan_tx_id, price)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        loan = self._get_loan(block, buyer_pk, loan_tx_id)
        self.assertEqual(loan.owner, buyer_pk)
        self.assertEqual(loan.paid_until_block, 5)  # a buyout moves no pointer
        self.assertEqual(
            block.accounts.get_account(owner, self.node).balance, 7 + price,
        )
        buyer = block.accounts.get_account(buyer_pk, self.node)
        self.assertEqual(buyer.balance, buyer_before - price - receipt.total_fee)

    def test_buy_price_rounds_up_by_the_plan_formula(self):
        block = self._make_block(height=5, fee=3, stake=1000)
        buyer_pk, buyer_key = seed_sender_account(block, balance=1_000_000)
        owner = os.urandom(32)
        block.accounts.set_account(owner, create_account(balance=0))
        _, loan_tx_id, _ = self._seed_loan(block, buyer_pk, owner=owner)

        receipt = self._buy(
            block, buyer_pk, buyer_key, loan_tx_id, self._price(fee=3, stake=1000),
        )

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        # ceil(100 * 1000 / 3) = 33334, not the floor 33333.
        self.assertEqual(
            block.accounts.get_account(owner, self.node).balance, 33334,
        )

    def test_buy_refunds_the_excess_to_the_buyer(self):
        block = self._make_block(height=5)
        buyer_pk, buyer_key = seed_sender_account(block, balance=1_000_000)
        owner = os.urandom(32)
        block.accounts.set_account(owner, create_account(balance=0))
        _, loan_tx_id, _ = self._seed_loan(block, buyer_pk, owner=owner)
        price = self._price()
        buyer_before = block.accounts.get_account(buyer_pk, self.node).balance

        receipt = self._buy(
            block, buyer_pk, buyer_key, loan_tx_id, price + 500,
        )

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        # The excess is not a charge: only the price and fees leave the buyer.
        buyer = block.accounts.get_account(buyer_pk, self.node)
        self.assertEqual(buyer.balance, buyer_before - price - receipt.total_fee)
        self.assertEqual(
            block.accounts.get_account(owner, self.node).balance, price,
        )

    def test_buy_from_the_treasury_credits_the_treasury(self):
        block = self._make_block(height=5)
        buyer_pk, buyer_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(block, buyer_pk)  # treasury owner
        price = self._price()

        receipt = self._buy(block, buyer_pk, buyer_key, loan_tx_id, price)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            TREASURY_BALANCE + price,
        )
        self.assertEqual(self._get_loan(block, buyer_pk, loan_tx_id).owner, buyer_pk)

    # --- failures ---

    def test_buy_below_the_price_fails(self):
        block = self._make_block(height=5)
        buyer_pk, buyer_key = seed_sender_account(block, balance=1_000_000)
        owner = os.urandom(32)
        block.accounts.set_account(owner, create_account(balance=7))
        _, loan_tx_id, _ = self._seed_loan(block, buyer_pk, owner=owner)

        receipt = self._buy(
            block, buyer_pk, buyer_key, loan_tx_id, self._price() - 1,
        )

        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(self._get_loan(block, buyer_pk, loan_tx_id).owner, owner)
        self.assertEqual(block.accounts.get_account(owner, self.node).balance, 7)

    def test_buy_by_the_current_owner_fails(self):
        block = self._make_block(height=5)
        buyer_pk, buyer_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(
            block, buyer_pk, owner=buyer_pk,
        )

        receipt = self._buy(block, buyer_pk, buyer_key, loan_tx_id, self._price())

        self.assertEqual(receipt.status, STATUS_FAILED)

    def test_buy_of_a_closed_loan_fails(self):
        block = self._make_block(height=5)
        buyer_pk, buyer_key = seed_sender_account(block, balance=1_000_000)
        owner = os.urandom(32)
        block.accounts.set_account(owner, create_account(balance=0))
        _, loan_tx_id, _ = self._seed_loan(
            block, buyer_pk, paid_until=0, owner=owner,
        )

        receipt = self._buy(block, buyer_pk, buyer_key, loan_tx_id, self._price())

        self.assertEqual(receipt.status, STATUS_FAILED)

    def test_buy_succeeds_with_the_price_but_less_than_the_amount(self):
        # Ruled semantics: the buyer needs only ``price`` in balance. Nothing
        # is escrowed, so a balance below ``transaction.amount`` is enough.
        block = self._make_block(height=5)
        price = self._price()
        buyer_pk, buyer_key = seed_sender_account(block, balance=price + 100_000)
        owner = os.urandom(32)
        block.accounts.set_account(owner, create_account(balance=0))
        _, loan_tx_id, _ = self._seed_loan(block, buyer_pk, owner=owner)
        buyer_before = block.accounts.get_account(buyer_pk, self.node).balance
        self.assertLess(buyer_before, price + 200_000)

        receipt = self._buy(block, buyer_pk, buyer_key, loan_tx_id, price + 200_000)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(
            block.accounts.get_account(buyer_pk, self.node).balance,
            buyer_before - price - receipt.total_fee,
        )
        self.assertEqual(
            block.accounts.get_account(owner, self.node).balance, price,
        )

    def test_buy_fails_with_one_unit_less_than_the_price(self):
        block = self._make_block(height=5)
        price = self._price()
        buyer_pk, buyer_key = seed_sender_account(block, balance=price - 1)
        owner = os.urandom(32)
        block.accounts.set_account(owner, create_account(balance=0))
        _, loan_tx_id, _ = self._seed_loan(block, buyer_pk, owner=owner)

        receipt = self._buy(block, buyer_pk, buyer_key, loan_tx_id, price)

        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(block.accounts.get_account(owner, self.node).balance, 0)

    def test_buy_from_a_missing_owner_account_fails(self):
        block = self._make_block(height=5)
        buyer_pk, buyer_key = seed_sender_account(block, balance=1_000_000)
        owner = os.urandom(32)  # no account seeded
        _, loan_tx_id, _ = self._seed_loan(block, buyer_pk, owner=owner)
        buyer_before = block.accounts.get_account(buyer_pk, self.node).balance

        receipt = self._buy(block, buyer_pk, buyer_key, loan_tx_id, self._price())

        self.assertEqual(receipt.status, STATUS_FAILED)
        # A failed tx keeps its fee but must not charge the price.
        self.assertEqual(
            block.accounts.get_account(buyer_pk, self.node).balance,
            buyer_before - receipt.total_fee,
        )

    def test_buy_handler_does_not_debit_before_the_owner_is_validated(self):
        # Direct call: ``apply`` would roll the debit back, hiding the order.
        block = self._make_block(height=5)
        buyer_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner = os.urandom(32)  # no account seeded
        _, loan_tx_id, _ = self._seed_loan(block, buyer_pk, owner=owner)
        buyer_before = block.accounts.get_account(buyer_pk, self.node).balance

        status = self._apply_buy_handler_only(
            block, buyer_pk, loan_tx_id, self._price(),
        )

        self.assertEqual(status, STATUS_FAILED)
        self.assertEqual(
            block.accounts.get_account(buyer_pk, self.node).balance, buyer_before,
        )

    def test_buyout_price_is_undefined_at_a_zero_fee(self):
        # ``all_time_rate_fraction`` returns ``(0, stake)`` for a zero-fee
        # previous block, so the handler reaches the division and the guard is
        # load-bearing. A full apply cannot observe the handler's failure
        # because the same block's storage-fee quote raises first, so pin the
        # guard directly.
        self.assertIsNone(_buyout_price(payment_amount=PAYMENT, rate_fraction=(0, 100_000)))
        self.assertIsNone(_buyout_price(payment_amount=PAYMENT, rate_fraction=(1000, 0)))
        self.assertIsNone(_buyout_price(payment_amount=0, rate_fraction=(1000, 100_000)))

    def test_buy_recipient_not_treasury_fails(self):
        block = self._make_block(height=5)
        buyer_pk, buyer_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(block, buyer_pk)

        receipt = self._buy(
            block, buyer_pk, buyer_key, loan_tx_id, self._price(),
            recipient=os.urandom(32),
        )

        self.assertEqual(receipt.status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
