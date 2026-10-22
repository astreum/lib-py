"""Validation tests for the STORAGE_PAYMENT (0x31) transaction code.

A storage-payment rewards a provider for hosting data by proving continued
availability: a challenged slot of the record, with a Merkle proof against the
record's ``slots_root``, and a proof of work over the slot's bytes.

Claim format (link list):
  Link(Bytes(storage_id),
    Link(Bytes(slot_expr_bytes),
      Link(<proof: list of Bytes(sibling hash)>,
        Link(Int(nonce),
          Link(Int(lane), NIL)))))

A record has two lanes; a claim names the lane it claims.
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

from dataclasses import replace

from blake3 import blake3

from astreum.consensus.transaction import apply_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.storage.model import (
    StorageLane,
    StorageRecord,
    storage_record_from_storage,
    storage_record_to_expr,
)
from astreum.consensus.transaction.storage.initial import generate_initial_storage_record
from astreum.consensus.transaction.storage.payment import (
    _leading_zero_bits,
    challenge_index as _challenge_index,
)
from astreum.crypto.bloom_search import ERA_SIZE
from astreum.expression.expr import _encode_int
from astreum.consensus.account import create_account
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
from astreum.expression.helpers import get_expr_builtin_tag


# ---------- helpers ----------

def _build_claim_expr(claim) -> Expr:
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
    payload = NIL
    for claim in reversed(claims):
        payload = link(_build_claim_expr(claim), payload)
    return payload


def _slots(n: int = 5) -> list[Expr]:
    return [bytes_(os.urandom(24)) for _ in range(n)]


def _claim_for(storage_id: bytes, slots: list[Expr], index: int, nonce: int = 0, lane: int = 0):
    """A claim on slot *index* of *slots* for *lane*, with its Merkle proof."""
    tree = create_merkle_tree([s.hash() for s in slots])
    return (
        storage_id,
        encode_expr_to_bytes(slots[index]),
        generate_merkle_tree_proof(tree, index),
        nonce,
        lane,
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

    def _reset(self, height: int = 1) -> None:
        """A fresh node and block at *height*."""
        self.node = _FakeNode()
        self.block = make_block(self.node, self.prev_block, height=height)
        seed_storage_account(self.block)

    def _seed_storage_record(
        self,
        atom_list_id: bytes,
        slots: list[Expr] | None = None,
        last_payment_block_hash: bytes | None = None,
        last_payment_height: int = 0,
        last_payment_winner: bytes = ZERO32,
        new_size: int = 100,
        new_count: int | None = None,
        lane1: StorageLane | None = None,
    ) -> tuple[StorageRecord, list[Expr]]:
        """Put a record over *slots* in the storage trie; *new_count* overrides its count.

        The ``last_payment_*`` arguments set lane 0. Lane 1 is *lane1*, or an
        unpaid lane with the same block hash.
        """
        slots = _slots() if slots is None else slots
        storage_account = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)
        lbh = last_payment_block_hash or get_block_expr(self.prev_block).hash()
        lane0 = StorageLane(
            block_hash=lbh, height=last_payment_height, winner=last_payment_winner
        )
        record = StorageRecord(
            creation_block_hash=lbh,
            lanes=(lane0, lane1 or StorageLane(block_hash=lbh, height=0, winner=ZERO32)),
            new_size=new_size,
            new_count=len(slots) if new_count is None else new_count,
            slots_root=create_merkle_tree([s.hash() for s in slots]).root,
        )
        record_head = store_expr_tree(self.node, storage_record_to_expr(record))
        put_in_radix_tree(storage_account.data, self.node, atom_list_id, record_head)
        for tn in storage_account.data.nodes.values():
            self.node.hot_storage[radix_node_hash(tn)] = get_radix_node_expr(tn)
        storage_account.data_hash = storage_account.data.root_hash or ZERO32
        return record, slots

    def _challenged(self, record: StorageRecord, storage_id: bytes, lane: int = 0) -> int:
        return _challenge_index(
            record.lanes[lane].block_hash, storage_id, record.new_count, lane
        )

    def _valid_claim(self, record, slots, storage_id, nonce=0, lane=0):
        return _claim_for(
            storage_id, slots, self._challenged(record, storage_id, lane), nonce, lane
        )

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
        return storage_record_from_storage(self.node, head.hash())

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
        self.assertEqual(updated.lanes[0].height, 1)
        self.assertEqual(updated.lanes[0].winner, sender_pk)
        self.assertEqual(updated.lanes[0].block_hash, self.block.previous_block_hash)

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
        claim = (os.urandom(32), encode_expr_to_bytes(int_(1)), [], 0, 0)
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
            self.assertEqual(self._record_in_trie(list_id).lanes[0].height, 1)

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
            # the one-lane format: (storage_id, slot, proof, nonce)
            link(bytes_(os.urandom(32)), link(bytes_(b"x"), link(NIL, link(int_(0), NIL)))),
            # proof entries that are not bytes
            link(bytes_(os.urandom(32)), link(bytes_(b"x"), link(link(int_(1), NIL), link(int_(0), link(int_(0), NIL))))),
            # nonce is not an int
            link(bytes_(os.urandom(32)), link(bytes_(b"x"), link(NIL, link(bytes_(b"0"), link(int_(0), NIL))))),
            # lane is not an int
            link(bytes_(os.urandom(32)), link(bytes_(b"x"), link(NIL, link(int_(0), link(bytes_(b"0"), NIL))))),
            # six items
            link(bytes_(os.urandom(32)), link(bytes_(b"x"), link(NIL, link(int_(0), link(int_(0), link(int_(0), NIL)))))),
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
        storage_id, _, proof, nonce, lane = self._valid_claim(record, slots, list_id)
        forged = (storage_id, encode_expr_to_bytes(bytes_(b"not in the record")), proof, nonce, lane)
        self._submit_claims(sender_pk, [forged])
        self.assertEqual(self._status(), STATUS_FAILED)

    def test_undecodable_expr_bytes_fail(self):
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(list_id, last_payment_winner=sender_pk)
        storage_id, _, proof, nonce, lane = self._valid_claim(record, slots, list_id)
        self._submit_claims(sender_pk, [(storage_id, b"\xff\xff\xff", proof, nonce, lane)])
        self.assertEqual(self._status(), STATUS_FAILED)

    def test_altered_proof_fails(self):
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(list_id, last_payment_winner=sender_pk)
        storage_id, slot_bytes, proof, nonce, lane = self._valid_claim(record, slots, list_id)
        self.assertTrue(proof)
        for bad_proof in (
            [bytes(32)] + proof[1:],
            proof[:-1],
            proof + [bytes(32)],
            list(reversed(proof)) if len(proof) > 1 and proof != list(reversed(proof)) else proof[:-1],
        ):
            with self.subTest(len=len(bad_proof)):
                self._submit_claims(sender_pk, [(storage_id, slot_bytes, bad_proof, nonce, lane)])
                self.assertEqual(self._status(), STATUS_FAILED)

    def test_proof_from_another_record_fails(self):
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        id_a, id_b = os.urandom(32), os.urandom(32)
        record_a, slots_a = self._seed_storage_record(id_a, last_payment_winner=sender_pk)
        record_b, slots_b = self._seed_storage_record(id_b, last_payment_winner=sender_pk)
        # A valid claim on A, presented against B
        _, slot_bytes, proof, nonce, lane = self._valid_claim(record_a, slots_a, id_a)
        self._submit_claims(sender_pk, [(id_b, slot_bytes, proof, nonce, lane)])
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
            true_index = _challenge_index(lbh, list_id, 5, 0)
            if true_index != _challenge_index(lbh, list_id, 6, 0):
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
        # make sender the incumbent of lane 0 → base_bits=0
        record.lanes = (replace(record.lanes[0], winner=sender_pk), record.lanes[1])
        record._expr = None

        put_in_radix_tree(
            storage_account.data, self.node, storage_id, storage_record_to_expr(record)
        )
        for tn in storage_account.data.nodes.values():
            self.node.hot_storage[radix_node_hash(tn)] = get_radix_node_expr(tn)
        storage_account.data_hash = storage_account.data.root_hash or ZERO32
        self.block.pending_exprs.append(storage_record_to_expr(record))
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
            0,
        )
        self._submit_claims(sender_pk, [claim])
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        expected_payout = record.new_size * (self.block.height - record.lanes[0].height)
        sender_after = self.block.accounts.get_account(sender_pk, self.node).balance
        self.assertEqual(sender_after, sender_before - receipt.total_fee + expected_payout)

    # --- lanes ---

    def _balance(self, pk):
        return self.block.accounts.get_account(pk, self.node).balance

    def test_two_providers_claim_different_lanes(self):
        """Two senders each hold a lane, claim it, and both get paid."""
        pk_a, _ = seed_sender_account(self.block, balance=1_000_000)
        pk_b, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        lbh = os.urandom(32)
        record, slots = self._seed_storage_record(
            list_id,
            last_payment_block_hash=lbh,
            last_payment_winner=pk_a,
            lane1=StorageLane(block_hash=lbh, height=0, winner=pk_b),
        )
        before_a, before_b = self._balance(pk_a), self._balance(pk_b)

        self._submit_claims(pk_a, [self._valid_claim(record, slots, list_id, lane=0)])
        flush_pending(self.node, self.block)
        receipt_a = self.block.receipts[-1]
        self.assertEqual(receipt_a.status, STATUS_SUCCESS)
        self._submit_claims(pk_b, [self._valid_claim(record, slots, list_id, lane=1)])
        receipt_b = self.block.receipts[-1]
        self.assertEqual(receipt_b.status, STATUS_SUCCESS)

        self.assertEqual(self._balance(pk_a), before_a - receipt_a.total_fee + 100)
        self.assertEqual(self._balance(pk_b), before_b - receipt_b.total_fee + 100)
        updated = self._record_in_trie(list_id)
        self.assertEqual([l.winner for l in updated.lanes], [pk_a, pk_b])
        self.assertEqual([l.height for l in updated.lanes], [1, 1])

    def test_same_sender_cannot_claim_both_lanes(self):
        """The lane 0 winner cannot take lane 1, even when lane 1 needs no work."""
        # Lane 1 is old enough to be free: a takeover needs no proof of work.
        height = 14 * ERA_SIZE
        self._reset(height)
        pk_a, _ = seed_sender_account(self.block, balance=1_000_000)
        pk_b, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(list_id, last_payment_winner=pk_a)

        self._submit_claims(pk_a, [self._valid_claim(record, slots, list_id, lane=1)])
        self.assertEqual(self._status(), STATUS_FAILED)
        self.assertEqual(self._record_in_trie(list_id).lanes[1].winner, ZERO32)

        # A different sender takes the same lane with the same proof.
        self._submit_claims(pk_b, [self._valid_claim(record, slots, list_id, lane=1)])
        self.assertEqual(self._status(), STATUS_SUCCESS)
        self.assertEqual(self._record_in_trie(list_id).lanes[1].winner, pk_b)

    def test_same_sender_two_lanes_in_one_tx(self):
        """Only the first claim pays: the second names the other lane of the same sender."""
        height = 14 * ERA_SIZE
        self._reset(height)
        pk_a, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(list_id, last_payment_winner=pk_a)
        before = self._balance(pk_a)

        self._submit_claims(pk_a, [
            self._valid_claim(record, slots, list_id, lane=0),
            self._valid_claim(record, slots, list_id, lane=1),
        ])
        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(self._balance(pk_a), before - receipt.total_fee + 100 * height)
        updated = self._record_in_trie(list_id)
        self.assertEqual(updated.lanes[0].winner, pk_a)
        self.assertEqual(updated.lanes[1].winner, ZERO32)

    def test_a_repeated_claim_on_one_lane_pays_once(self):
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        lbh = os.urandom(32)
        record, slots = self._seed_storage_record(
            list_id,
            last_payment_block_hash=lbh,
            lane1=StorageLane(block_hash=lbh, height=0, winner=sender_pk),
        )
        before = self._balance(sender_pk)
        claim = self._valid_claim(record, slots, list_id, lane=1)
        self._submit_claims(sender_pk, [claim, claim])
        receipt = self.block.receipts[-1]
        self.assertEqual(self._balance(sender_pk), before - receipt.total_fee + 100)
        self.assertEqual(self._record_in_trie(list_id).lanes[1].height, 1)

    def test_lanes_challenge_different_indices(self):
        storage_id, block_hash = b"\x01" * 32, b"\x02" * 32
        indices = {
            _challenge_index(block_hash, storage_id, 1_000_000, lane) for lane in (0, 1)
        }
        self.assertEqual(len(indices), 2)

    def test_work_for_one_lane_fails_on_other(self):
        """A nonce found for lane 0 does not pay on lane 1."""
        height = 7 * ERA_SIZE  # 7 eras old: the takeover needs fib(6) = 8 bits
        self._reset(height)
        list_id, lbh = os.urandom(32), os.urandom(32)
        slots = _slots(1)  # one slot: both lanes challenge it, with the same proof
        pk, _ = seed_sender_account(self.block, balance=1_000_000)
        slot_bytes = encode_expr_to_bytes(slots[0])
        slot_id = slots[0].hash()

        def work(nonce, lane):
            return blake3(
                lbh + pk + list_id + slot_id + slot_bytes + _encode_int(nonce) + bytes([lane])
            ).digest()

        nonce = 0
        while not (_leading_zero_bits(work(nonce, 0)) >= 8 and _leading_zero_bits(work(nonce, 1)) < 8):
            nonce += 1

        def run(lane):
            self._reset(height)
            self.block.accounts.set_account(pk, create_account(balance=1_000_000))
            lane_state = lambda w: StorageLane(block_hash=lbh, height=0, winner=w)
            self._seed_storage_record(
                list_id, slots=slots, last_payment_block_hash=lbh,
                last_payment_winner=os.urandom(32), lane1=lane_state(os.urandom(32)),
            )
            self._submit_claims(pk, [_claim_for(list_id, slots, 0, nonce, lane)])
            return self._status()

        self.assertEqual(run(0), STATUS_SUCCESS)
        self.assertEqual(run(1), STATUS_FAILED)

    def test_payout_uses_lane_height(self):
        self._reset(10)
        pk_a, _ = seed_sender_account(self.block, balance=1_000_000)
        pk_b, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id, lbh = os.urandom(32), os.urandom(32)
        record, slots = self._seed_storage_record(
            list_id,
            last_payment_block_hash=lbh,
            last_payment_height=2,
            last_payment_winner=pk_a,
            lane1=StorageLane(block_hash=lbh, height=6, winner=pk_b),
        )
        before_a, before_b = self._balance(pk_a), self._balance(pk_b)
        self._submit_claims(pk_a, [self._valid_claim(record, slots, list_id, lane=0)])
        fee_a = self.block.receipts[-1].total_fee
        flush_pending(self.node, self.block)
        self._submit_claims(pk_b, [self._valid_claim(record, slots, list_id, lane=1)])
        fee_b = self.block.receipts[-1].total_fee
        self.assertEqual(self._balance(pk_a), before_a - fee_a + 100 * (10 - 2))
        self.assertEqual(self._balance(pk_b), before_b - fee_b + 100 * (10 - 6))

    def test_takeover_penalty_per_lane(self):
        """The penalty uses the claimed lane: a fresh lane 0 is hard, an old lane 1 is free."""
        height = 14 * ERA_SIZE
        self._reset(height)
        pk, _ = seed_sender_account(self.block, balance=1_000_000)
        pk2, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id, lbh = os.urandom(32), os.urandom(32)
        record, slots = self._seed_storage_record(
            list_id,
            last_payment_block_hash=lbh,
            last_payment_height=height - 1,  # 0 eras old: 233 bits
            last_payment_winner=os.urandom(32),
            lane1=StorageLane(block_hash=lbh, height=0, winner=os.urandom(32)),  # 14 eras: 0 bits
        )
        self._submit_claims(pk, [self._valid_claim(record, slots, list_id, lane=0)])
        self.assertEqual(self._status(), STATUS_FAILED)
        self._submit_claims(pk2, [self._valid_claim(record, slots, list_id, lane=1)])
        self.assertEqual(self._status(), STATUS_SUCCESS)

    def test_invalid_lane_rejected(self):
        """Lane 2 and a missing lane fail to parse. Lanes 0 and 1 parse."""
        from astreum.consensus.transaction.storage.payment import _parse_claim

        claim = _claim_for(os.urandom(32), _slots(), 0)
        for lane in (0, 1):
            parsed = _parse_claim(_build_claim_expr((*claim[:4], lane)))
            self.assertIsNotNone(parsed, lane)
            self.assertEqual(parsed[4], lane)
        for lane in (2, 255):
            self.assertIsNone(_parse_claim(_build_claim_expr((*claim[:4], lane))), lane)
        storage_id, slot_bytes, proof, nonce, _ = claim
        no_lane = link(
            bytes_(storage_id),
            link(bytes_(slot_bytes), link(bytes_list_to_expr(proof), link(int_(nonce), NIL))),
        )
        self.assertIsNone(_parse_claim(no_lane))

    def test_claim_with_lane_two_does_not_pay(self):
        sender_pk, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id = os.urandom(32)
        record, slots = self._seed_storage_record(list_id, last_payment_winner=sender_pk)
        good = self._valid_claim(record, slots, list_id, lane=0)
        self._submit_claims(sender_pk, [(*good[:4], 2)])
        self.assertEqual(self._status(), STATUS_FAILED)
        self.assertEqual(self._record_in_trie(list_id).lanes[0].height, 0)

    def test_claim_rewrites_only_claimed_lane(self):
        pk_a, _ = seed_sender_account(self.block, balance=1_000_000)
        pk_b, _ = seed_sender_account(self.block, balance=1_000_000)
        list_id, lbh = os.urandom(32), os.urandom(32)
        record, slots = self._seed_storage_record(
            list_id,
            last_payment_block_hash=lbh,
            last_payment_winner=pk_a,
            lane1=StorageLane(block_hash=lbh, height=0, winner=pk_b),
        )
        self._submit_claims(pk_b, [self._valid_claim(record, slots, list_id, lane=1)])
        self.assertEqual(self._status(), STATUS_SUCCESS)
        updated = self._record_in_trie(list_id)
        self.assertEqual(updated.lanes[0], record.lanes[0])
        self.assertEqual(
            updated.lanes[1],
            StorageLane(block_hash=self.block.previous_block_hash, height=1, winner=pk_b),
        )
        for name in ("slots_root", "new_count", "new_size", "creation_block_hash"):
            self.assertEqual(getattr(updated, name), getattr(record, name), name)


def _all_cells(expr):
    stack, out = [expr], []
    while stack:
        e = stack.pop()
        if e is None:
            continue
        out.append(e)
        if get_expr_builtin_tag(e) == "link":
            stack.extend((e.head, e.tail))
    return out


if __name__ == "__main__":
    unittest.main()
