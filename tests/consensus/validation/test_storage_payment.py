"""Validation tests for the STORAGE_PAYMENT (0x31) transaction code.

A storage-payment rewards a provider for hosting data by proving continued
availability: a challenged slot of the record, with a Merkle proof against the
record's ``slots_root``, and a proof of work over the slot's bytes.

Claim format (link list):
  Link(Bytes(storage_id),
    Link(Bytes(slot_expr_bytes),
      Link(<proof: list of Bytes(sibling hash)>,
        Link(Int(nonce), NIL))))
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
from astreum.consensus.transaction.storage.model import StorageRecord
from astreum.consensus.transaction.storage.initial import generate_initial_storage_record
from astreum.consensus.transaction.storage.payment import challenge_index as _challenge_index
from astreum.crypto.merkle import create_merkle_tree, generate_merkle_tree_proof
from astreum.expression.encoding import encode_expr_to_bytes
from astreum.expression.helpers import bytes_list_to_expr
from astreum.storage.radix import (
    get_from_radix_tree,
    get_radix_node_expr,
    put_in_radix_tree,
)
from astreum.storage.radix.node import radix_node_hash
from astreum.expression import Expr, ZERO32, NIL, int_, bytes_, link
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

def _build_claim_expr(claim) -> Expr:
    storage_id, slot_bytes, proof, nonce = claim
    return link(
        bytes_(storage_id),
        link(
            bytes_(slot_bytes),
            link(bytes_list_to_expr(proof), link(int_(nonce), NIL)),
        ),
    )


def _build_claim_payload(claims) -> Expr:
    payload = NIL
    for claim in reversed(claims):
        payload = link(_build_claim_expr(claim), payload)
    return payload


def _slots(n: int = 5) -> list[Expr]:
    return [bytes_(os.urandom(24)) for _ in range(n)]


def _claim_for(storage_id: bytes, slots: list[Expr], index: int, nonce: int = 0):
    """A claim on slot *index* of *slots*, with its Merkle proof."""
    tree = create_merkle_tree([s.hash() for s in slots])
    return (
        storage_id,
        encode_expr_to_bytes(slots[index]),
        generate_merkle_tree_proof(tree, index),
        nonce,
    )


# ---------- test class ----------

class TestStoragePayment(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self.block = make_block(self.node, self.prev_block)
        seed_storage_account(self.block)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _seed_storage_record(
        self,
        atom_list_id: bytes,
        slots: list[Expr] | None = None,
        last_payment_block_hash: bytes | None = None,
        last_payment_height: int = 0,
        last_payment_winner: bytes = ZERO32,
        new_size: int = 100,
        new_count: int | None = None,
    ) -> tuple[StorageRecord, list[Expr]]:
        """Put a record over *slots* in the storage trie; *new_count* overrides its count."""
        slots = _slots() if slots is None else slots
        storage_account = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)
        lbh = last_payment_block_hash or get_block_expr(self.prev_block).hash()
        record = StorageRecord(
            creation_block_hash=lbh,
            last_payment_block_hash=lbh,
            last_payment_height=last_payment_height,
            last_payment_winner=last_payment_winner,
            new_size=new_size,
            new_count=len(slots) if new_count is None else new_count,
            slots_root=create_merkle_tree([s.hash() for s in slots]).root,
        )
        record_head = store_expr_tree(self.node, record.expr())
        put_in_radix_tree(storage_account.data, self.node, atom_list_id, record_head)
        for tn in storage_account.data.nodes.values():
            self.node.hot_storage[radix_node_hash(tn)] = get_radix_node_expr(tn)
        storage_account.data_hash = storage_account.data.root_hash or ZERO32
        return record, slots

    def _challenged(self, record: StorageRecord, storage_id: bytes) -> int:
        return _challenge_index(record.last_payment_block_hash, storage_id, record.new_count)

    def _valid_claim(self, record, slots, storage_id, nonce=0):
        return _claim_for(storage_id, slots, self._challenged(record, storage_id), nonce)

    def _submit_claims(self, sender_pk: bytes, claims, **kw) -> bytes:
        payload = _build_claim_payload(claims)
        store_expr_tree(self.node, payload)
        return self._submit_tx(sender_pk, payload, **kw)

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

    def _status(self) -> int:
        return self.block.receipts[-1].status

    def _record_in_trie(self, list_id: bytes) -> StorageRecord:
        flush_pending(self.node, self.block)
        storage_account = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)
        head = get_from_radix_tree(storage_account.data, self.node, list_id)
        self.assertIsNotNone(head)
        return StorageRecord.from_storage(self.node, head.hash())

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    # --- success paths ---

    def test_successful_payment_incumbent(self):
        """Incumbent reclaim: base_bits=0, nonce=0 suffices."""
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(
            list_id, last_payment_winner=sender_pk, new_size=100
        )
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance

        self._submit_claims(sender_pk, [self._valid_claim(record, slots, list_id)])
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        expected_payout = 100 * (1 - 0)
        sender_after = self.block.accounts.get_account(sender_pk, self.node).balance
        fees = receipt.transaction_fee + receipt.storage_fee
        self.assertEqual(sender_after, sender_before - fees + expected_payout)

        updated = self._record_in_trie(list_id)
        self.assertEqual(updated.last_payment_height, 1)
        self.assertEqual(updated.last_payment_winner, sender_pk)

    def test_payment_keeps_slots_root_count_size_and_creation(self):
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(list_id, last_payment_winner=sender_pk)
        self._submit_claims(sender_pk, [self._valid_claim(record, slots, list_id)])
        self.assertEqual(self._status(), STATUS_SUCCESS)
        updated = self._record_in_trie(list_id)
        for name in ("slots_root", "new_count", "new_size", "creation_block_hash"):
            self.assertEqual(getattr(updated, name), getattr(record, name), name)

    def test_verifying_a_claim_fetches_nothing(self):
        from unittest.mock import patch

        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(list_id, last_payment_winner=sender_pk)
        claim = self._valid_claim(record, slots, list_id)
        # The slots are held nowhere: the claim carries the one that is checked.
        for slot in slots:
            self.assertNotIn(slot.hash(), self.node.hot_storage)
        with patch("astreum.storage.exprs.network.get_exprs_from_network") as fetch:
            self._submit_claims(sender_pk, [claim])
        self.assertEqual(self._status(), STATUS_SUCCESS)
        fetch.assert_not_called()

    def test_every_slot_can_be_the_challenged_one(self):
        """Whatever the count, the proof for the challenged index verifies."""
        for count in (1, 2, 3, 4, 5, 8, 9):
            with self.subTest(count=count):
                node = _FakeNode()
                self.node, self.block = node, make_block(node, self.prev_block)
                seed_storage_account(self.block)
                sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
                list_id = os.urandom(32)
                record, slots = self._seed_storage_record(
                    list_id, slots=_slots(count), last_payment_winner=sender_pk
                )
                self._submit_claims(sender_pk, [self._valid_claim(record, slots, list_id)])
                self.assertEqual(self._status(), STATUS_SUCCESS)

    def test_zero_balance_incumbent_claim_succeeds(self):
        """A zero-balance sender can claim; the payout covers the tx's own fees."""
        sender_pk, _ = seed_sender_account(self.block, balance=0)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(
            list_id, last_payment_winner=sender_pk, new_size=20000
        )
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance
        self.assertEqual(sender_before, 0)

        self._submit_claims(sender_pk, [self._valid_claim(record, slots, list_id)])
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        expected_payout = 20000 * (1 - 0)
        sender_after = self.block.accounts.get_account(sender_pk, self.node).balance
        fees = receipt.transaction_fee + receipt.storage_fee
        self.assertEqual(sender_after, sender_before - fees + expected_payout)

    def test_zero_balance_invalid_claim_soft_fails(self):
        """A zero-balance sender with an invalid claim soft-fails (no raise)."""
        sender_pk, _ = seed_sender_account(self.block, balance=0)
        claim = (os.urandom(32), encode_expr_to_bytes(int_(1)), [], 0)
        self._submit_claims(sender_pk, [claim])

        self.assertEqual(self._status(), STATUS_FAILED)
        sender_after = self.block.accounts.get_account(sender_pk, self.node).balance
        self.assertEqual(sender_after, 0)

    def test_wrong_recipient_succeeds_without_payment(self):
        """Recipient != STORAGE_ADDRESS → handler skipped, receipt stays SUCCESS."""
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        self._submit_tx(sender_pk, NIL, recipient=os.urandom(32))
        self.assertEqual(self._status(), STATUS_SUCCESS)

    def test_a_repeated_claim_in_one_tx_pays_once(self):
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(
            list_id, last_payment_winner=sender_pk, new_size=100
        )
        before = self.block.accounts.get_account(sender_pk, self.node).balance
        claim = self._valid_claim(record, slots, list_id)
        self._submit_claims(sender_pk, [claim, claim, claim])
        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        after = self.block.accounts.get_account(sender_pk, self.node).balance
        self.assertEqual(after, before - receipt.total_fee + 100)

    def test_two_records_in_one_tx_each_pay(self):
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        ids = [os.urandom(32), os.urandom(32)]
        claims = []
        for list_id, size in zip(ids, (100, 300)):
            record, slots = self._seed_storage_record(
                list_id, last_payment_winner=sender_pk, new_size=size
            )
            claims.append(self._valid_claim(record, slots, list_id))
        before = self.block.accounts.get_account(sender_pk, self.node).balance
        self._submit_claims(sender_pk, claims)
        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        after = self.block.accounts.get_account(sender_pk, self.node).balance
        self.assertEqual(after, before - receipt.total_fee + 400)
        for list_id in ids:
            self.assertEqual(self._record_in_trie(list_id).last_payment_height, 1)

    # --- failure paths ---

    def test_empty_claim_list_fails(self):
        """NIL payload → no parsed claims → fails."""
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        self._submit_tx(sender_pk, NIL)
        self.assertEqual(self._status(), STATUS_FAILED)

    def test_no_contract_fails(self):
        """storage_id not in storage trie → claim skipped → fails."""
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        slots = _slots()
        self._submit_claims(sender_pk, [_claim_for(os.urandom(32), slots, 0)])
        self.assertEqual(self._status(), STATUS_FAILED)

    def test_malformed_claim_fails(self):
        """A claim not in the claim format → parse fails."""
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        for bad in (
            link(int_(42), NIL),
            # the retired format: (storage_id, slot_id, nonce)
            link(bytes_(os.urandom(32)), link(bytes_(os.urandom(32)), link(int_(0), NIL))),
            # proof entries that are not bytes
            link(bytes_(os.urandom(32)), link(bytes_(b"x"), link(link(int_(1), NIL), link(int_(0), NIL)))),
            # nonce is not an int
            link(bytes_(os.urandom(32)), link(bytes_(b"x"), link(NIL, link(bytes_(b"0"), NIL)))),
        ):
            with self.subTest(bad=bad.hash().hex()[:8]):
                self._submit_tx(sender_pk, link(bad, NIL))
                self.assertEqual(self._status(), STATUS_FAILED)

    def test_proof_for_another_slot_fails(self):
        """The expr is in the record but not at the challenged index."""
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(list_id, last_payment_winner=sender_pk)
        wrong = (self._challenged(record, list_id) + 1) % len(slots)
        self._submit_claims(sender_pk, [_claim_for(list_id, slots, wrong)])
        self.assertEqual(self._status(), STATUS_FAILED)

    def test_wrong_expr_with_a_valid_proof_fails(self):
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(list_id, last_payment_winner=sender_pk)
        storage_id, _, proof, nonce = self._valid_claim(record, slots, list_id)
        forged = (storage_id, encode_expr_to_bytes(bytes_(b"not in the record")), proof, nonce)
        self._submit_claims(sender_pk, [forged])
        self.assertEqual(self._status(), STATUS_FAILED)

    def test_undecodable_expr_bytes_fail(self):
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(list_id, last_payment_winner=sender_pk)
        storage_id, _, proof, nonce = self._valid_claim(record, slots, list_id)
        self._submit_claims(sender_pk, [(storage_id, b"\xff\xff\xff", proof, nonce)])
        self.assertEqual(self._status(), STATUS_FAILED)

    def test_altered_proof_fails(self):
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(list_id, last_payment_winner=sender_pk)
        storage_id, slot_bytes, proof, nonce = self._valid_claim(record, slots, list_id)
        self.assertTrue(proof)
        for bad_proof in (
            [bytes(32)] + proof[1:],
            proof[:-1],
            proof + [bytes(32)],
            list(reversed(proof)) if len(proof) > 1 and proof != list(reversed(proof)) else proof[:-1],
        ):
            with self.subTest(len=len(bad_proof)):
                self._submit_claims(sender_pk, [(storage_id, slot_bytes, bad_proof, nonce)])
                self.assertEqual(self._status(), STATUS_FAILED)

    def test_proof_from_another_record_fails(self):
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        id_a, id_b = os.urandom(32), os.urandom(32)
        record_a, slots_a = self._seed_storage_record(id_a, last_payment_winner=sender_pk)
        record_b, slots_b = self._seed_storage_record(id_b, last_payment_winner=sender_pk)
        # A valid claim on A, presented against B
        _, slot_bytes, proof, nonce = self._valid_claim(record_a, slots_a, id_a)
        self._submit_claims(sender_pk, [(id_b, slot_bytes, proof, nonce)])
        self.assertEqual(self._status(), STATUS_FAILED)

    def test_wrong_new_count_fails(self):
        """A claim built for the record's slots under another count challenges
        another index than the record's count does, so its proof fails."""
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        slots = _slots(5)
        # a last-payment block for which counts 5 and 6 challenge different slots
        while True:
            lbh = os.urandom(32)
            true_index = _challenge_index(lbh, list_id, 5)
            if true_index != _challenge_index(lbh, list_id, 6):
                break
        self._seed_storage_record(
            list_id,
            slots=slots,
            last_payment_block_hash=lbh,
            last_payment_winner=sender_pk,
            new_count=6,
        )
        self._submit_claims(sender_pk, [_claim_for(list_id, slots, true_index)])
        self.assertEqual(self._status(), STATUS_FAILED)

    def test_zero_count_fails(self):
        """new_count=0 → rejected, no division by zero."""
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        slots = _slots()
        self._seed_storage_record(
            list_id, slots=slots, last_payment_winner=sender_pk, new_count=0
        )
        self._submit_claims(sender_pk, [_claim_for(list_id, slots, 0)])
        self.assertEqual(self._status(), STATUS_FAILED)

    def test_zero_size_fails(self):
        """new_size=0 → payout ≤ 0 → skipped → fails."""
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(
            list_id, last_payment_winner=sender_pk, new_size=0
        )
        self._submit_claims(sender_pk, [self._valid_claim(record, slots, list_id)])
        self.assertEqual(self._status(), STATUS_FAILED)

    def test_insufficient_pow_fails(self):
        """Not-incumbent, era=0 → fib(13)=233 bits required → nonce=0 fails."""
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        # last_payment_winner=ZERO32 → new sender is NOT incumbent
        record, slots = self._seed_storage_record(list_id, last_payment_winner=ZERO32)
        self._submit_claims(sender_pk, [self._valid_claim(record, slots, list_id)])
        self.assertEqual(self._status(), STATUS_FAILED)

    def test_network_record_claim(self):
        """Claim against the record of a real block expr pays the claimer."""
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance
        storage_account = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)

        prev_expr = get_block_expr(self.prev_block)
        storage_id = prev_expr.hash()
        store_expr_tree(self.node, prev_expr)

        result = generate_initial_storage_record(
            self.node, self.block, prev_expr, follow_refs=True
        )
        self.assertIsNotNone(result)
        record, slot_hashes, _, _ = result
        record.last_payment_winner = sender_pk  # make sender the incumbent → base_bits=0
        record._expr = None

        put_in_radix_tree(storage_account.data, self.node, storage_id, record.expr())
        for tn in storage_account.data.nodes.values():
            self.node.hot_storage[radix_node_hash(tn)] = get_radix_node_expr(tn)
        storage_account.data_hash = storage_account.data.root_hash or ZERO32
        self.block.pending_exprs.append(record.expr())
        flush_pending(self.node, self.block)

        # The claim names the challenged slot's cell, with a proof from the walk's list.
        index = self._challenged(record, storage_id)
        by_hash = {e.hash(): e for e in _all_cells(prev_expr)}
        slot_expr = by_hash[slot_hashes[index]]
        tree = create_merkle_tree(slot_hashes)
        claim = (
            storage_id,
            encode_expr_to_bytes(slot_expr),
            generate_merkle_tree_proof(tree, index),
            0,
        )
        self._submit_claims(sender_pk, [claim])
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        expected_payout = record.new_size * (self.block.height - record.last_payment_height)
        sender_after = self.block.accounts.get_account(sender_pk, self.node).balance
        self.assertEqual(sender_after, sender_before - receipt.total_fee + expected_payout)


def _all_cells(expr):
    stack, out = [expr], []
    while stack:
        e = stack.pop()
        if e is None:
            continue
        out.append(e)
        if e._tag == "link":
            stack.extend((e._head, e._tail))
    return out


if __name__ == "__main__":
    unittest.main()
