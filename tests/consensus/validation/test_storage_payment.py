"""Validation tests for the STORAGE_PAYMENT (0x31) transaction code.

A storage-payment rewards a provider for hosting atom data by proving
continued availability via a challenge-response PoW mechanism.

Claim format (link list):
  Link(Bytes(storage_id),
    Link(Bytes(slot_id),
      Link(Int(nonce), NIL)))
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
HELPERS_DIR = Path(__file__).resolve().parent
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from blake3 import blake3

from astreum.consensus.transaction import apply_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.storage.model import StorageRecord, StorageSlot
from astreum.consensus.transaction.storage.initial import generate_initial_storage_record
from astreum.storage.radix import (
    get_from_radix_tree,
    get_radix_node_expr,
    put_in_radix_tree,
)
from astreum.storage.radix.node import radix_node_hash
from astreum.expression import Expr, ZERO32, NIL, int_, fp64_, bytes_, str_, symbol, link
from astreum.consensus.block.encoding.expr import get_block_expr
from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS

from _helpers import (
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_sender_account,
    seed_storage_account,
    store_expr_tree,
    store_tx,
)


# ---------- helpers ----------

def _build_claim_expr(
    storage_id: bytes,
    slot_id: bytes,
    nonce: int,
) -> Expr:
    return link(
        bytes_(storage_id),
        link(
            bytes_(slot_id),
            link(int_(nonce), NIL),
        ),
    )


def _build_claim_payload(claims: list[tuple[bytes, bytes, int]]) -> Expr:
    payload = NIL
    for record_id, slot_id, nonce in reversed(claims):
        claim = _build_claim_expr(record_id, slot_id, nonce)
        payload = link(claim, payload)
    return payload


def _compute_challenge_index(
    last_payment_block_hash: bytes,
    storage_id: bytes,
    new_count: int,
) -> int:
    seed = blake3(last_payment_block_hash + storage_id).digest()
    return int.from_bytes(seed[:8], "little", signed=False) % new_count


# ---------- test class ----------

class TestStoragePayment(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self.block = make_block(self.node, self.prev_block)
        self.block.total_mint = 0
        seed_storage_account(self.block)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _seed_storage_record(
        self,
        atom_list_id: bytes,
        last_payment_block_hash: bytes | None = None,
        last_payment_height: int = 0,
        last_payment_winner: bytes = ZERO32,
        new_size: int = 100,
        new_count: int = 3,
        mint: bool = False,
    ) -> StorageRecord:
        storage_account = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)
        lbh = last_payment_block_hash or get_block_expr(self.prev_block).hash()
        record = StorageRecord(
            creation_block_hash=lbh,
            last_payment_block_hash=lbh,
            last_payment_height=last_payment_height,
            last_payment_winner=last_payment_winner,
            new_size=new_size,
            new_count=new_count,
            mint=mint,
        )
        record_head = store_expr_tree(self.node, record.expr())
        put_in_radix_tree(storage_account.data, self.node, atom_list_id, record_head)
        for tn in storage_account.data.nodes.values():
            self.node.hot_storage[radix_node_hash(tn)] = get_radix_node_expr(tn)
        storage_account.data_hash = storage_account.data.root_hash or ZERO32
        return record

    def _seed_storage_slot(
        self,
        storage_id: bytes,
        sequence: int,
    ) -> bytes:
        slot = StorageSlot(
            storage_id=storage_id,
            sequence=sequence,
        )
        slot_hash = store_expr_tree(self.node, slot.expr())
        # Slot expr stays in hot_storage (read by both StorageSlot.from_storage
        # and _get_expr_from_local_storage via the same key).
        # _get_expr_from_local_storage returns the Slot's Link expr;
        # its .to_bytes() is fed into the work hash, which is deterministic.
        # For incumbent (base_bits=0) this is fine since any hash passes.
        # For failure tests, the nonce doesn't satisfy the required bits anyway.
        return slot_hash

    def _build_successful_claim(
        self,
        storage_id: bytes,
        sender_pk: bytes,
        last_payment_block_hash: bytes,
        new_count: int,
    ) -> tuple[bytes, int]:
        """Build a valid claim payload (nonce=0, incumbent)."""
        challenge_index = _compute_challenge_index(
            last_payment_block_hash, storage_id, new_count,
        )
        slot_id = self._seed_storage_slot(storage_id, challenge_index)
        # Incumbent → base_bits=0, so nonce=0 always works
        payload = _build_claim_payload([(storage_id, slot_id, 0)])
        store_expr_tree(self.node, payload)
        return payload, challenge_index

    def _submit_tx(
        self,
        sender_pk: bytes,
        payload: Expr,
        recipient: bytes = STORAGE_ADDRESS,
        amount: int = 0,
    ) -> bytes:
        tx = Transaction(
            chain_id=1,
            amount=amount,
            code=TransactionCode.STORAGE_PAYMENT,
            counter=0,
            cost_limit=0,
            data=payload,
            recipient=recipient,
            sender=sender_pk,
        )
        tx_hash = store_tx(self.node, tx)
        apply_transaction(self.node, self.block, tx_hash)
        return tx_hash

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    # --- success paths ---

    def test_successful_payment_incumbent(self):
        """Incumbent reclaim: base_bits=0, nonce=0 suffices."""
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record = self._seed_storage_record(
            atom_list_id=list_id,
            last_payment_winner=sender_pk,
            last_payment_height=0,
            new_size=100,
            new_count=3,
            mint=True,
        )
        lbh = record.last_payment_block_hash

        payload, _ = self._build_successful_claim(list_id, sender_pk, lbh, 3)
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance

        self._submit_tx(sender_pk, payload)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        expected_payout = 100 * (1 - 0)
        sender_after = self.block.accounts.get_account(sender_pk, self.node).balance
        fees = receipt.transaction_fee + receipt.storage_fee
        self.assertEqual(sender_after, sender_before - fees + expected_payout)
        self.assertEqual(receipt.mint, expected_payout)

        # StorageRecord updated in storage trie
        storage_account = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)
        updated_head = get_from_radix_tree(storage_account.data, self.node, list_id)
        self.assertIsNotNone(updated_head)
        updated = StorageRecord.from_storage(self.node, updated_head.hash())
        self.assertIsNotNone(updated)
        self.assertEqual(updated.last_payment_height, 1)
        self.assertEqual(updated.last_payment_winner, sender_pk)

    def test_zero_balance_incumbent_claim_succeeds(self):
        """A zero-balance sender can claim; the payout covers the tx's own fees."""
        sender_pk, sender_key = seed_sender_account(self.block, balance=0)
        list_id = os.urandom(32)
        record = self._seed_storage_record(
            atom_list_id=list_id,
            last_payment_winner=sender_pk,
            last_payment_height=0,
            new_size=2000,
            new_count=3,
            mint=True,
        )
        lbh = record.last_payment_block_hash

        payload, _ = self._build_successful_claim(list_id, sender_pk, lbh, 3)
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance
        self.assertEqual(sender_before, 0)

        self._submit_tx(sender_pk, payload)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        expected_payout = 2000 * (1 - 0)
        sender_after = self.block.accounts.get_account(sender_pk, self.node).balance
        fees = receipt.transaction_fee + receipt.storage_fee
        self.assertEqual(sender_after, sender_before - fees + expected_payout)
        self.assertEqual(receipt.mint, expected_payout)

    def test_zero_balance_invalid_claim_soft_fails(self):
        """A zero-balance sender with an invalid claim soft-fails (no raise)."""
        sender_pk, sender_key = seed_sender_account(self.block, balance=0)
        missing_id = os.urandom(32)
        slot_id = os.urandom(32)
        payload = _build_claim_payload([(missing_id, slot_id, 0)])
        store_expr_tree(self.node, payload)

        self._submit_tx(sender_pk, payload)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_FAILED)
        sender_after = self.block.accounts.get_account(sender_pk, self.node).balance
        self.assertEqual(sender_after, 0)

    def test_wrong_recipient_succeeds_without_payment(self):
        """Recipient != STORAGE_ADDRESS → handler skipped, receipt stays SUCCESS."""
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        other = os.urandom(32)

        tx = Transaction(
            chain_id=1,
            amount=0,
            code=TransactionCode.STORAGE_PAYMENT,
            counter=0,
            cost_limit=0,
            data=NIL,
            recipient=other,
            sender=sender_pk,
        )
        tx_hash = store_tx(self.node, tx)
        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)

    # --- failure paths ---

    def test_empty_claim_list_fails(self):
        """NIL payload → no parsed claims → fails."""
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        self._submit_tx(sender_pk, NIL)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_no_contract_fails(self):
        """storage_id not in storage trie → claim skipped → fails."""
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        missing_id = os.urandom(32)
        slot_id = os.urandom(32)
        payload = _build_claim_payload([(missing_id, slot_id, 0)])
        store_expr_tree(self.node, payload)
        self._submit_tx(sender_pk, payload)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_malformed_claim_fails(self):
        """Claim not in Link(Bytes, Link(Bytes, Link(Int, NIL))) format → parse fails."""
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        # malformed: Link(Int(42), NIL) — not a valid claim
        bad_claim = link(int_(42), NIL)
        payload = link(bad_claim, NIL)
        store_expr_tree(self.node, payload)
        self._submit_tx(sender_pk, payload)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_wrong_slot_sequence_fails(self):
        """Slot.sequence != challenge_index → skipped."""
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record = self._seed_storage_record(
            atom_list_id=list_id,
            last_payment_winner=sender_pk,
            last_payment_height=0,
        )
        lbh = record.last_payment_block_hash
        challenge_index = _compute_challenge_index(lbh, list_id, 3)
        wrong_sequence = (challenge_index + 1) % 3
        slot_id = self._seed_storage_slot(list_id, wrong_sequence)

        payload = _build_claim_payload([(list_id, slot_id, 0)])
        store_expr_tree(self.node, payload)
        self._submit_tx(sender_pk, payload)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_zero_count_fails(self):
        """new_count=0 → division by zero → caught → fails."""
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        self._seed_storage_record(
            atom_list_id=list_id,
            last_payment_winner=sender_pk,
            new_count=0,
        )

        slot_id = self._seed_storage_slot(list_id, 0)
        payload = _build_claim_payload([(list_id, slot_id, 0)])
        store_expr_tree(self.node, payload)
        self._submit_tx(sender_pk, payload)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_zero_size_fails(self):
        """new_size=0 → payout ≤ 0 → skipped → fails."""
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        self._seed_storage_record(
            atom_list_id=list_id,
            last_payment_winner=sender_pk,
            new_size=0,
            new_count=3,
        )
        lbh = get_block_expr(self.prev_block).hash()
        challenge_index = _compute_challenge_index(lbh, list_id, 3)
        slot_id = self._seed_storage_slot(list_id, challenge_index)
        payload = _build_claim_payload([(list_id, slot_id, 0)])
        store_expr_tree(self.node, payload)
        self._submit_tx(sender_pk, payload)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_data_not_found_fails(self):
        """Data not in hot_storage → _get_expr_from_local_storage returns None."""
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record = self._seed_storage_record(
            atom_list_id=list_id,
            last_payment_winner=sender_pk,
        )
        lbh = record.last_payment_block_hash
        challenge_index = _compute_challenge_index(lbh, list_id, 3)
        slot_id = os.urandom(32)  # arbitrary — no slot or data stored anywhere

        payload = _build_claim_payload([(list_id, slot_id, 0)])
        store_expr_tree(self.node, payload)
        self._submit_tx(sender_pk, payload)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_insufficient_pow_fails(self):
        """Not-incumbent, era=0 → fib(13)=233 bits required → nonce=0 fails."""
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        # last_payment_winner=ZERO32 → new sender is NOT incumbent
        self._seed_storage_record(
            atom_list_id=list_id,
            last_payment_winner=ZERO32,
            last_payment_height=0,
            new_size=100,
            new_count=3,
        )
        lbh = get_block_expr(self.prev_block).hash()
        challenge_index = _compute_challenge_index(lbh, list_id, 3)
        slot_id = self._seed_storage_slot(list_id, challenge_index)
        payload = _build_claim_payload([(list_id, slot_id, 0)])
        store_expr_tree(self.node, payload)
        self._submit_tx(sender_pk, payload)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_minted_network_record_claim(self):
        """Claim against a network minted block-storage record → receipt.mint == payout."""
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        storage_account = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)

        storage_id = get_block_expr(self.prev_block).hash()

        # Commit prev_block expr to storage (replicating transactions.py:205-217)
        result = generate_initial_storage_record(self.node, self.block, get_block_expr(self.prev_block))
        self.assertIsNotNone(result)
        record, slot_map, _, _ = result
        record.mint = True
        record.last_payment_winner = sender_pk  # make sender the incumbent → base_bits=0

        put_in_radix_tree(storage_account.data, self.node, storage_id, record.expr())
        for h, slot in slot_map.items():
            put_in_radix_tree(storage_account.data, self.node, h, slot.expr())
        for tn in storage_account.data.nodes.values():
            self.node.hot_storage[radix_node_hash(tn)] = get_radix_node_expr(tn)
        storage_account.data_hash = storage_account.data.root_hash or ZERO32
        self.block.pending_exprs.append(record.expr())
        for slot in slot_map.values():
            self.block.pending_exprs.append(slot.expr())
        flush_pending(self.node, self.block)

        # Use the first slot from slot_map, patching its sequence to match challenge_index
        lbh = record.last_payment_block_hash
        challenge_index = int.from_bytes(
            blake3(lbh + storage_id).digest()[:8], "little", signed=False
        ) % record.new_count
        first_expr_id, first_slot = next(iter(slot_map.items()))
        first_slot.sequence = challenge_index
        first_slot._expr = None  # invalidate cache so expr() regenerates
        slot_id = first_slot.expr().hash()
        # Re-store the updated slot expr in the radix tree
        put_in_radix_tree(storage_account.data, self.node, first_expr_id, first_slot.expr())
        for tn in storage_account.data.nodes.values():
            self.node.hot_storage[radix_node_hash(tn)] = get_radix_node_expr(tn)
        self.block.pending_exprs.append(first_slot.expr())

        # Build claim
        payload = _build_claim_payload([(storage_id, slot_id, 0)])
        store_expr_tree(self.node, payload)

        flush_pending(self.node, self.block)
        self._submit_tx(sender_pk, payload)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        expected_payout = record.new_size * (self.block.height - record.last_payment_height)
        self.assertEqual(receipt.mint, expected_payout)


if __name__ == "__main__":
    unittest.main()
