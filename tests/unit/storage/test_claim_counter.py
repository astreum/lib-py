import sys
import unittest
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.storage.model import StorageLane, StorageRecord
from astreum.expression import ZERO32
from astreum.storage.workers import claim as claim_mod
from astreum.storage.workers.claim_flow import InFlight
from astreum.storage.workers.claim_schedule import ClaimQueue, LaneChoice, pow_budget
from astreum.storage.workers.claim import (
    _build_multi_claim_tx,
    _chain_counter,
    _claim_step,
    _send_batch,
)
from astreum.utils.config import NodeConfig


def _make_keys():
    secret = ed25519.Ed25519PrivateKey.generate()
    public = secret.public_key()
    return secret, public


def _public_bytes(public):
    return public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


class _AccountsStub:
    def __init__(self, account=None, raises=False):
        self.account = account
        self.raises = raises

    def get_account(self, address=None, node=None):
        if self.raises:
            raise RuntimeError("trie fetch failed")
        return self.account


def _make_node(storage_public_key_bytes, accounts, chain_id=1):
    node = SimpleNamespace(
        config=NodeConfig(chain_id=chain_id),
        storage_public_key_bytes=storage_public_key_bytes,
        storage_secret_key=None,
        latest_block=SimpleNamespace(accounts=accounts, height=40),
        logger=SimpleNamespace(info=lambda *a, **k: None, exception=lambda *a, **k: None),
    )
    return node


class TestChainCounter(unittest.TestCase):
    def setUp(self):
        self.secret, self.public = _make_keys()
        self.pk = _public_bytes(self.public)

    def test_chain_counter(self):
        node = _make_node(self.pk, _AccountsStub(account=SimpleNamespace(counter=7)))
        self.assertEqual(_chain_counter(node), 7)

    def test_missing_account_is_zero(self):
        node = _make_node(self.pk, _AccountsStub(account=None))
        self.assertEqual(_chain_counter(node), 0)

    def test_fetch_failure_is_zero(self):
        node = _make_node(self.pk, _AccountsStub(raises=True))
        self.assertEqual(_chain_counter(node), 0)

    def test_reads_the_given_block(self):
        node = _make_node(self.pk, _AccountsStub(account=SimpleNamespace(counter=7)))
        other = SimpleNamespace(accounts=_AccountsStub(account=SimpleNamespace(counter=9)))
        self.assertEqual(_chain_counter(node, other), 9)

    def test_no_latest_block_is_zero(self):
        node = _make_node(self.pk, _AccountsStub(account=SimpleNamespace(counter=6)))
        node.latest_block = None
        self.assertEqual(_chain_counter(node), 0)


def _claim(nonce, seed=0, slot_size=40, proof_len=3, lane=0):
    """A claim: (storage_id, slot_expr_bytes, proof, nonce, lane)."""
    return (
        bytes([seed]) * 32,
        bytes([seed + 1]) * slot_size,
        [bytes([seed, k]) * 16 for k in range(proof_len)],
        nonce,
        lane,
    )


class TestBuildMultiClaimTx(unittest.TestCase):
    def setUp(self):
        self.secret, self.public = _make_keys()
        self.pk = _public_bytes(self.public)
        self.node = _make_node(self.pk, _AccountsStub())
        self.node.storage_secret_key = self.secret

    def test_uses_given_counter(self):
        tx = _build_multi_claim_tx(self.node, [_claim(42)], self.secret, 11)
        self.assertEqual(tx.counter, 11)

    def test_signed_and_well_formed(self):
        tx = _build_multi_claim_tx(self.node, [_claim(42)], self.secret, 0)
        self.assertEqual(tx.code, TransactionCode.STORAGE_PAYMENT)
        self.assertEqual(tx.sender, self.pk)
        self.assertTrue(tx.signature)
        self.assertEqual(tx.recipient, b"\x00" * 32)

    def test_consecutive_counters(self):
        tx1 = _build_multi_claim_tx(self.node, [_claim(1)], self.secret, 3)
        tx2 = _build_multi_claim_tx(self.node, [_claim(2)], self.secret, 4)
        self.assertEqual((tx1.counter, tx2.counter), (3, 4))
        self.assertNotEqual(tx1.expr().hash(), tx2.expr().hash())


class TestSendBatch(unittest.TestCase):
    """Send gate: send only if sum(payouts) - cost reaches the minimum profit."""

    def setUp(self):
        self.secret, self.public = _make_keys()
        self.pk = _public_bytes(self.public)
        self.node = _make_node(self.pk, _AccountsStub())
        self.node.storage_secret_key = self.secret
        self.head = SimpleNamespace(height=40)

    def _entries(self, payout, count=1):
        return [(*_claim(i, seed=i), payout) for i in range(count)]

    def _send(self, entries, min_profit=0, fee=10):
        self.node.config.storage_claim_min_profit = min_profit
        with patch.object(claim_mod, "storage_fee_at_rate", return_value=fee), patch(
            "astreum.consensus.transaction.send.send_transaction"
        ) as mock_send:
            result, self.tx_bytes = _send_batch(self.node, self.head, entries, 5, Fraction(1))
        return result, mock_send

    def test_send_batch_profitable(self):
        entries = self._entries(payout=1000)
        result, mock_send = self._send(entries)
        self.assertEqual(result, InFlight(5, 40, entries, tx_bytes=result.tx_bytes))
        self.assertGreater(result.tx_bytes, 0)
        self.assertEqual(self.tx_bytes, result.tx_bytes)
        mock_send.assert_called_once()
        tx = mock_send.call_args.args[1]
        self.assertEqual(tx.code, TransactionCode.STORAGE_PAYMENT)
        self.assertEqual(tx.counter, 5)

    def test_send_batch_sent_height_is_the_head_at_the_send(self):
        self.node.latest_block.height = 43
        entries = self._entries(payout=1000)
        result, _ = self._send(entries)
        self.assertEqual(result.sent_height, 43)

    def test_send_batch_reads_the_counter_at_the_send_block(self):
        self.node.latest_block.accounts = _AccountsStub(account=SimpleNamespace(counter=7))
        self.node.config.storage_claim_min_profit = 0
        with patch.object(claim_mod, "storage_fee_at_rate", return_value=10), patch(
            "astreum.consensus.transaction.send.send_transaction"
        ) as mock_send:
            result, _ = _send_batch(self.node, self.head, self._entries(payout=1000), None, Fraction(1))
        self.assertEqual(result.counter, 7)
        self.assertEqual(mock_send.call_args.args[1].counter, 7)

    def test_send_batch_unprofitable(self):
        result, mock_send = self._send(self._entries(payout=1))
        self.assertIsNone(result)
        mock_send.assert_not_called()

    def test_an_unprofitable_batch_still_gives_its_size(self):
        self._send(self._entries(payout=1000))
        sent_bytes = self.tx_bytes
        result, _ = self._send(self._entries(payout=1))
        self.assertIsNone(result)
        self.assertGreater(sent_bytes, 0)
        self.assertEqual(self.tx_bytes, sent_bytes)

    def test_send_batch_below_min_profit(self):
        cost = 1 + 10 + 10
        entries = self._entries(payout=cost + 5)
        result, mock_send = self._send(entries, min_profit=10)
        self.assertIsNone(result)
        mock_send.assert_not_called()
        result, mock_send = self._send(entries, min_profit=5)
        self.assertIsInstance(result, InFlight)
        mock_send.assert_called_once()

    def test_send_batch_aggregates_payouts(self):
        result, mock_send = self._send(self._entries(payout=100, count=3))
        self.assertIsNotNone(result)
        mock_send.assert_called_once()

    def test_send_batch_send_failure(self):
        self.node.config.storage_claim_min_profit = 0
        with patch.object(claim_mod, "storage_fee_at_rate", return_value=10), patch(
            "astreum.consensus.transaction.send.send_transaction", side_effect=RuntimeError("down")
        ):
            result, _ = _send_batch(self.node, self.head, self._entries(payout=1000), 5, Fraction(1))
        self.assertIsNone(result)


class TestComputeClaim(unittest.TestCase):
    """_build_claims: the slot, its proof and a PoW nonce, from the records table."""

    def setUp(self):
        from astreum.expression import bytes_
        from astreum.crypto.merkle import create_merkle_tree

        self.pk = _public_bytes(_make_keys()[1])
        self.storage_id = b"\x21" * 32
        self.slots = [bytes_(bytes([i]) * 20) for i in range(7)]
        self.ids = [s.hash() for s in self.slots]
        self.root = create_merkle_tree(self.ids).root
        self.node = SimpleNamespace(
            storage_public_key_bytes=self.pk,
            storage_claim_hash_rate=1000.0,
            latest_block=SimpleNamespace(height=5, storage_difficulty=0),
        )

    def _record(self, **kw):
        fields = dict(
            lanes=(
                # incumbent: no work needed
                StorageLane(block_hash=b"\x11" * 32, height=0, winner=self.pk),
                StorageLane(block_hash=b"\x12" * 32, height=0, winner=ZERO32),
            ),
            new_count=len(self.slots),
            new_size=100,
            slots_root=self.root,
        )
        fields.update(kw)
        return StorageRecord(**fields)

    def _run(self, record, blob=None, local=True, lane=0, bits=0):
        blob = b"".join(self.ids) if blob is None else blob
        by_id = {s.hash(): s for s in self.slots}
        with patch.object(
            claim_mod, "get_records_from_cold_storage", return_value={self.storage_id: blob}
        ), patch.object(claim_mod, "get_expr_from_hot_storage", return_value=None), patch.object(
            claim_mod,
            "get_exprs_from_cold_storage",
            side_effect=lambda n, ids: {i: by_id[i] for i in ids if local and i in by_id},
        ):
            return claim_mod._build_claims(
                self.node,
                self.node.latest_block,
                [(self.storage_id, record, LaneChoice(lane, bits))],
                1 << 20,
                lambda: False,
            )

    def test_claim_verifies_against_the_records_root(self):
        from astreum.crypto.merkle import MerkleTree, verify_merkle_tree_proof
        from astreum.expression.encoding import decode_expr_from_bytes
        from astreum.consensus.transaction.storage.payment import challenge_index

        record = self._record()
        entries = self._run(record)
        self.assertEqual(len(entries), 1)
        storage_id, slot_bytes, proof, nonce, lane, payout = entries[0]
        self.assertEqual(storage_id, self.storage_id)
        self.assertEqual(lane, 0)
        self.assertEqual(payout, 100 * 5)
        index = challenge_index(record.lanes[0].block_hash, self.storage_id, record.new_count, 0)
        self.assertEqual(decode_expr_from_bytes(slot_bytes).hash(), self.ids[index])
        self.assertTrue(
            verify_merkle_tree_proof(
                MerkleTree(self.root, record.new_count), index, self.ids[index], proof
            )
        )

    def test_nonce_meets_the_bits(self):
        from astreum.consensus.transaction.storage.payment import _leading_zero_bits
        from astreum.expression.expr import _encode_int
        from blake3 import blake3

        record = self._record()
        (storage_id, slot_bytes, _proof, nonce, lane, _payout), = self._run(record, bits=8)
        slot_id = self.ids[
            claim_mod._challenge_index(record.lanes[0].block_hash, storage_id, record.new_count, 0)
        ]
        work = blake3(
            record.lanes[0].block_hash + self.pk + storage_id + slot_id + slot_bytes
            + _encode_int(nonce) + bytes([lane])
        ).digest()
        self.assertGreaterEqual(_leading_zero_bits(work), 8)

    def test_no_slot_list_no_claim(self):
        with patch.object(claim_mod, "get_records_from_cold_storage", return_value={}), patch.object(
            claim_mod, "get_expr_from_hot_storage", return_value=None
        ), patch.object(claim_mod, "get_exprs_from_cold_storage", return_value={}):
            self.assertEqual(
                claim_mod._build_claims(
                    self.node,
                    self.node.latest_block,
                    [(self.storage_id, self._record(), LaneChoice(0, 0))],
                    10,
                    lambda: False,
                ),
                [],
            )

    def test_a_slot_list_that_does_not_give_the_root_is_not_claimed(self):
        blob = b"".join(reversed(self.ids))
        self.assertEqual(self._run(self._record(), blob=blob), [])

    def test_a_list_of_the_wrong_length_is_not_claimed(self):
        self.assertEqual(self._run(self._record(), blob=b"".join(self.ids[:-1])), [])

    def test_slot_data_not_held_no_claim(self):
        self.assertEqual(self._run(self._record(), local=False), [])

    def test_empty_record_no_claim(self):
        self.assertEqual(self._run(self._record(new_count=0)), [])

    def test_build_two_records_one_get_each(self):
        other_id = b"\x22" * 32
        blob = b"".join(self.ids)
        by_id = {s.hash(): s for s in self.slots}
        plans = [
            (self.storage_id, self._record(), LaneChoice(0, 0)),
            (other_id, self._record(), LaneChoice(0, 0)),
        ]
        with patch.object(
            claim_mod,
            "get_records_from_cold_storage",
            return_value={self.storage_id: blob, other_id: blob},
        ) as get_records, patch.object(
            claim_mod, "get_expr_from_hot_storage", return_value=None
        ), patch.object(
            claim_mod,
            "get_exprs_from_cold_storage",
            side_effect=lambda n, ids: {i: by_id[i] for i in ids},
        ) as get_exprs:
            entries = claim_mod._build_claims(
                self.node, self.node.latest_block, plans, 10, lambda: False
            )
        self.assertEqual([e[0] for e in entries], [self.storage_id, other_id])
        get_records.assert_called_once()
        get_exprs.assert_called_once()

    def test_find_nonce_stops_on_stop_event(self):
        with patch.object(claim_mod.time, "sleep") as sleep:
            result = claim_mod._find_nonce(self.node, b"p", 0, 256, 1 << 20, lambda: True)
        self.assertIsNone(result)
        sleep.assert_not_called()

    def test_find_nonce_updates_hash_rate(self):
        self.node.storage_claim_hash_rate = 1.0  # one nonce a chunk
        stops = iter([False, True])
        result = claim_mod._find_nonce(self.node, b"p", 0, 256, 1 << 20, lambda: next(stops))
        self.assertIsNone(result)
        self.assertGreater(self.node.storage_claim_hash_rate, 1.0)

    def test_find_nonce_budget(self):
        with patch.object(claim_mod, "blake3") as hasher:
            hasher.return_value.digest.return_value = b"\xff" * 32
            result = claim_mod._find_nonce(self.node, b"p", 0, 256, 10, lambda: False)
        self.assertIsNone(result)
        self.assertEqual(hasher.call_count, 10)

    def test_find_nonce_yields_after_each_chunk(self):
        stops = iter([False, False, True])
        with patch.object(claim_mod.time, "sleep") as sleep:
            claim_mod._find_nonce(self.node, b"p", 0, 256, 1 << 30, lambda: next(stops))
        self.assertEqual(sleep.call_count, 2)

    def test_measure_hash_rate_positive(self):
        self.assertGreater(claim_mod.measure_hash_rate(), 0)


class TestSplitClaims(unittest.TestCase):
    """Bundles are cut near MAX_CLAIM_TX_BYTES so a tx fits one response page."""

    def _entries(self, count, slot_size):
        return [(*_claim(i, seed=i, slot_size=slot_size), 10) for i in range(count)]

    def _group_bytes(self, group):
        from astreum.expression import NIL, link

        expr = NIL
        for entry in reversed(group):
            expr = link(claim_mod._claim_expr(entry[:5]), expr)
        return claim_mod._tree_bytes(expr)

    def test_small_claims_share_one_tx(self):
        groups = claim_mod._split_claims(self._entries(5, 100))
        self.assertEqual([len(g) for g in groups], [5])

    def test_big_claims_split_into_several_txs(self):
        entries = self._entries(7, 20_000)
        groups = claim_mod._split_claims(entries)
        self.assertGreater(len(groups), 1)
        self.assertEqual([e for g in groups for e in g], entries)  # order and content kept
        for g in groups:
            self.assertLessEqual(self._group_bytes(g), claim_mod.MAX_CLAIM_TX_BYTES + 200)

    def test_a_claim_over_the_limit_gets_its_own_tx(self):
        entries = self._entries(1, 100) + self._entries(1, 70_000) + self._entries(1, 100)
        groups = claim_mod._split_claims(entries)
        self.assertEqual([len(g) for g in groups], [1, 1, 1])

    def test_no_entries_no_groups(self):
        self.assertEqual(claim_mod._split_claims([]), [])


class TestClaimStep(unittest.TestCase):
    def setUp(self):
        self.pk = _public_bytes(_make_keys()[1])
        self.other_a = b"\x02" * 32
        self.other_b = b"\x03" * 32
        self.storage_id = b"\x0c" * 32
        self.node = SimpleNamespace(
            config=NodeConfig(chain_id=1, storage_claim_min_profit=0),
            storage_public_key_bytes=self.pk,
            storage_claim_band=1,
            storage_claim_hash_rate=1000.0,
            storage_claim_inclusion_delay=1,
            logger=SimpleNamespace(info=lambda *a, **k: None, exception=lambda *a, **k: None),
        )
        self.head = SimpleNamespace(height=60, storage_difficulty=0)
        self.queue = ClaimQueue()
        self.held = {}

    def _record(self, winner0, winner1, height0=0, height1=0):
        return StorageRecord(
            new_size=100,
            lanes=(
                StorageLane(block_hash=b"\x11" * 32, height=height0, winner=winner0),
                StorageLane(block_hash=b"\x12" * 32, height=height1, winner=winner1),
            ),
            new_count=1,
        )

    def _step(self, record, build=None, interval=50, radix=True):
        head_expr = SimpleNamespace(hash=lambda: b"\x0e" * 32) if radix else None
        build = build or MagicMock(return_value=[])
        with patch.object(claim_mod, "get_from_radix_tree", return_value=head_expr), patch.object(
            claim_mod, "storage_record_from_storage", return_value=record
        ), patch.object(claim_mod, "fair_interval", return_value=interval), patch.object(
            claim_mod, "_build_claims", build
        ):
            entries = _claim_step(
                self.node,
                self.head,
                object(),
                [self.storage_id],
                self.queue,
                self.held,
                Fraction(0),
                100,
                lambda: False,
            )
        return entries, build

    def test_step_holder_not_due_pushed(self):
        record = self._record(self.pk, self.other_a, height0=30)
        entries, build = self._step(record)
        self.assertEqual(entries, [])
        self.assertEqual(self.queue.heap, [(79, self.storage_id)])
        build.assert_not_called()

    def test_step_builds_due_claim(self):
        record = self._record(self.pk, self.other_a)
        entry = (self.storage_id, b"s", [b"p"], 3, 0, 6000)
        entries, build = self._step(record, build=MagicMock(return_value=[entry]))
        self.assertEqual(entries, [entry])
        self.assertEqual(self.queue.heap, [])
        self.assertIn(self.storage_id, self.held)
        plans = build.call_args.args[2]
        self.assertEqual(plans, [(self.storage_id, record, LaneChoice(0, 0))])

    def test_step_waits_for_profit(self):
        self.node.config.storage_claim_min_profit = 10_000
        record = self._record(self.pk, self.other_a)
        entries, _ = self._step(record)
        self.assertEqual(entries, [])
        self.assertEqual(self.queue.heap, [(101, self.storage_id)])

    def test_step_drops_held_when_lane_lost(self):
        self.held[self.storage_id] = (100, 3)
        entries, _ = self._step(self._record(self.other_a, self.other_b, height0=59, height1=59))
        self.assertEqual(entries, [])
        self.assertNotIn(self.storage_id, self.held)

    def test_step_records_held_lane(self):
        from astreum.consensus.transaction.storage.difficulty import shared_prefix_bits

        self._step(self._record(self.pk, self.other_a, height0=30))
        self.assertEqual(self.held[self.storage_id], (100, shared_prefix_bits(self.pk, self.storage_id)))

    def test_step_missing_record_pushed_back(self):
        entries, build = self._step(None, radix=False)
        self.assertEqual(entries, [])
        self.assertEqual(self.queue.heap, [(110, self.storage_id)])
        build.assert_not_called()

    def test_step_missing_record_drops_held(self):
        self.held[self.storage_id] = (100, 3)
        self._step(None, radix=False)
        self.assertNotIn(self.storage_id, self.held)

    def test_step_read_failure_pushed_back_and_others_handled(self):
        failing = b"\x0d" * 32
        record = self._record(self.pk, self.other_a, height0=30)
        self.held[failing] = (100, 3)

        def radix(tree, node, storage_id):
            if storage_id == failing:
                raise RuntimeError("read failed")
            return SimpleNamespace(hash=lambda: b"\x0e" * 32)

        with patch.object(claim_mod, "get_from_radix_tree", side_effect=radix), patch.object(
            claim_mod, "storage_record_from_storage", return_value=record
        ), patch.object(claim_mod, "fair_interval", return_value=50), patch.object(
            claim_mod, "_build_claims", MagicMock(return_value=[])
        ):
            entries = _claim_step(
                self.node,
                self.head,
                object(),
                [failing, self.storage_id],
                self.queue,
                self.held,
                Fraction(0),
                100,
                lambda: False,
            )
        self.assertEqual(entries, [])
        self.assertEqual(sorted(self.queue.heap), [(79, self.storage_id), (110, failing)])
        self.assertNotIn(failing, self.held)
        self.assertIn(self.storage_id, self.held)

    def test_step_one_budget_for_the_whole_step(self):
        other_id = b"\x0d" * 32
        record = self._record(self.pk, self.other_a)
        entry_a = (self.storage_id, b"s", [b"p"], 3, 0, 6000)
        entry_b = (other_id, b"s", [b"p"], 4, 0, 6000)
        build = MagicMock(return_value=[entry_a, entry_b])
        with patch.object(
            claim_mod, "get_from_radix_tree", return_value=SimpleNamespace(hash=lambda: b"\x0e" * 32)
        ), patch.object(claim_mod, "storage_record_from_storage", return_value=record), patch.object(
            claim_mod, "fair_interval", return_value=50
        ), patch.object(claim_mod, "_build_claims", build):
            entries = _claim_step(
                self.node,
                self.head,
                object(),
                [self.storage_id, other_id],
                self.queue,
                self.held,
                Fraction(0),
                100,
                lambda: False,
            )
        self.assertEqual(entries, [entry_a, entry_b])
        build.assert_called_once()
        budget = pow_budget(self.node.storage_claim_hash_rate, 50, 2)
        self.assertEqual(build.call_args.args[3], budget)
        self.assertEqual(self.queue.heap, [])

    def test_step_unbuilt_claim_pushed_back(self):
        record = self._record(self.pk, self.other_a)
        entries, build = self._step(record)
        self.assertEqual(entries, [])
        build.assert_called_once()
        self.assertEqual(self.queue.heap, [(110, self.storage_id)])


if __name__ == "__main__":
    unittest.main()
