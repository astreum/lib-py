import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, "src")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from astreum.consensus.transaction.code import TransactionCode
from astreum.storage.workers import claim as claim_mod
from astreum.storage.workers.claim import (
    _BASE_TX_FEE,
    _build_claims_for_records,
    _build_multi_claim_tx,
    _next_claim_counter,
    _submit_claims,
)


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
        config={"chain_id": chain_id},
        storage_public_key_bytes=storage_public_key_bytes,
        storage_secret_key=None,
        latest_block=SimpleNamespace(accounts=accounts),
        logger=SimpleNamespace(info=lambda *a, **k: None, exception=lambda *a, **k: None),
    )
    return node


class TestNextClaimCounter(unittest.TestCase):
    def setUp(self):
        self.secret, self.public = _make_keys()
        self.pk = _public_bytes(self.public)

    def test_on_chain_counter_used(self):
        account = SimpleNamespace(counter=7)
        node = _make_node(self.pk, _AccountsStub(account=account))
        self.assertEqual(_next_claim_counter(node), 7)

    def test_local_wins_when_ahead(self):
        account = SimpleNamespace(counter=3)
        node = _make_node(self.pk, _AccountsStub(account=account))
        node.next_claim_counter = 4
        self.assertEqual(_next_claim_counter(node), 4)

    def test_on_chain_wins_when_ahead(self):
        account = SimpleNamespace(counter=9)
        node = _make_node(self.pk, _AccountsStub(account=account))
        node.next_claim_counter = 2
        self.assertEqual(_next_claim_counter(node), 9)

    def test_missing_account_is_zero(self):
        node = _make_node(self.pk, _AccountsStub(account=None))
        self.assertEqual(_next_claim_counter(node), 0)

    def test_fetch_failure_falls_back_to_local(self):
        node = _make_node(self.pk, _AccountsStub(raises=True))
        node.next_claim_counter = 5
        self.assertEqual(_next_claim_counter(node), 5)

    def test_fetch_failure_without_local_is_zero(self):
        node = _make_node(self.pk, _AccountsStub(raises=True))
        self.assertEqual(_next_claim_counter(node), 0)

    def test_no_latest_block_is_zero(self):
        node = _make_node(self.pk, _AccountsStub(account=SimpleNamespace(counter=6)))
        node.latest_block = None
        self.assertEqual(_next_claim_counter(node), 0)


def _claim(nonce, seed=0, slot_size=40, proof_len=3):
    """A claim: (storage_id, slot_expr_bytes, proof, nonce)."""
    return (
        bytes([seed]) * 32,
        bytes([seed + 1]) * slot_size,
        [bytes([seed, k]) * 16 for k in range(proof_len)],
        nonce,
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


class TestSubmitClaimsGate(unittest.TestCase):
    """Aggregate cost gate: send only if sum(payouts) covers the tx cost."""

    def setUp(self):
        self.secret, self.public = _make_keys()
        self.pk = _public_bytes(self.public)
        self.node = _make_node(self.pk, _AccountsStub())
        self.node.storage_secret_key = self.secret
        self.node.next_claim_counter = 5

    def _entries(self, payout, count=1):
        return [(*_claim(i, seed=i), payout) for i in range(count)]

    def test_profitable_bundle_sent(self):
        entries = self._entries(payout=1000)
        with patch.object(claim_mod, "calculate_storage_fee", return_value=10), patch(
            "astreum.consensus.transaction.send.send_transaction"
        ) as mock_send:
            _submit_claims(self.node, entries)
        mock_send.assert_called_once()
        args, _ = mock_send.call_args
        self.assertEqual(args[1].code, TransactionCode.STORAGE_PAYMENT)
        self.assertEqual(self.node.next_claim_counter, 6)

    def test_unprofitable_bundle_dropped(self):
        entries = self._entries(payout=1)
        with patch.object(claim_mod, "calculate_storage_fee", return_value=10), patch(
            "astreum.consensus.transaction.send.send_transaction"
        ) as mock_send:
            _submit_claims(self.node, entries)
        mock_send.assert_not_called()
        self.assertEqual(self.node.next_claim_counter, 5)

    def test_fee_error_skips_pass(self):
        entries = self._entries(payout=1000)
        with patch.object(
            claim_mod, "calculate_storage_fee", side_effect=ValueError("no fees")
        ), patch(
            "astreum.consensus.transaction.send.send_transaction"
        ) as mock_send:
            _submit_claims(self.node, entries)
        mock_send.assert_not_called()
        self.assertEqual(self.node.next_claim_counter, 5)

    def test_no_latest_block_noop(self):
        self.node.latest_block = None
        with patch.object(claim_mod, "calculate_storage_fee", return_value=10), patch(
            "astreum.consensus.transaction.send.send_transaction"
        ) as mock_send:
            _submit_claims(self.node, self._entries(payout=1000))
        mock_send.assert_not_called()

    def test_empty_entries_noop(self):
        with patch.object(claim_mod, "calculate_storage_fee", return_value=10), patch(
            "astreum.consensus.transaction.send.send_transaction"
        ) as mock_send:
            _submit_claims(self.node, [])
        mock_send.assert_not_called()

    def test_each_tx_of_a_split_bundle_is_gated_and_sent_on_its_own(self):
        entries = [(*_claim(i, seed=i, slot_size=30_000), 1000) for i in range(4)]
        with patch.object(claim_mod, "calculate_storage_fee", return_value=10), patch(
            "astreum.consensus.transaction.send.send_transaction"
        ) as mock_send:
            _submit_claims(self.node, entries)
        self.assertGreater(mock_send.call_count, 1)
        counters = [c.args[1].counter for c in mock_send.call_args_list]
        self.assertEqual(counters, list(range(5, 5 + len(counters))))
        self.assertEqual(self.node.next_claim_counter, 5 + len(counters))

    def test_an_unprofitable_tx_does_not_stop_the_next_one(self):
        entries = [(*_claim(0, seed=0, slot_size=30_000), 1)] + [
            (*_claim(i, seed=i, slot_size=30_000), 100_000) for i in (1, 2)
        ]
        with patch.object(claim_mod, "calculate_storage_fee", return_value=10), patch(
            "astreum.consensus.transaction.send.send_transaction"
        ) as mock_send:
            _submit_claims(self.node, entries)
        self.assertGreaterEqual(mock_send.call_count, 1)

    def test_multi_claim_bundle_aggregates_payouts(self):
        entries = self._entries(payout=100, count=3)  # 300 total
        with patch.object(claim_mod, "calculate_storage_fee", return_value=10), patch(
            "astreum.consensus.transaction.send.send_transaction"
        ) as mock_send:
            _submit_claims(self.node, entries)
        mock_send.assert_called_once()
        args, _ = mock_send.call_args
        self.assertEqual(args[1].code, TransactionCode.STORAGE_PAYMENT)
        self.assertEqual(self.node.next_claim_counter, 6)


class TestComputeClaim(unittest.TestCase):
    """_compute_pow_and_challenge: the slot, its proof and a PoW nonce, from the records table."""

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
            latest_block=SimpleNamespace(height=5),
        )

    def _record(self, **kw):
        fields = dict(
            last_payment_block_hash=b"\x11" * 32,
            last_payment_winner=self.pk,  # incumbent: no work needed
            last_payment_height=0,
            new_count=len(self.slots),
            new_size=100,
            slots_root=self.root,
        )
        fields.update(kw)
        return SimpleNamespace(**fields)

    def _run(self, record, blob=None, local=True):
        blob = b"".join(self.ids) if blob is None else blob
        by_id = {s.hash(): s for s in self.slots}
        with patch.object(claim_mod, "get_record_from_cold_storage", return_value=blob), patch.object(
            claim_mod, "get_expr_from_local_storage", side_effect=lambda n, h: by_id.get(h) if local else None
        ):
            return claim_mod._compute_pow_and_challenge(self.node, self.storage_id, record)

    def test_claim_verifies_against_the_records_root(self):
        from astreum.crypto.merkle import MerkleTree, verify_merkle_tree_proof
        from astreum.expression.encoding import decode_expr_from_bytes
        from astreum.consensus.transaction.storage.payment import challenge_index

        record = self._record()
        storage_id, slot_bytes, proof, nonce = self._run(record)
        self.assertEqual(storage_id, self.storage_id)
        index = challenge_index(record.last_payment_block_hash, self.storage_id, record.new_count)
        self.assertEqual(decode_expr_from_bytes(slot_bytes).hash(), self.ids[index])
        self.assertTrue(
            verify_merkle_tree_proof(
                MerkleTree(self.root, record.new_count), index, self.ids[index], proof
            )
        )

    def test_no_slot_list_no_claim(self):
        with patch.object(claim_mod, "get_record_from_cold_storage", return_value=None):
            self.assertIsNone(
                claim_mod._compute_pow_and_challenge(self.node, self.storage_id, self._record())
            )

    def test_a_slot_list_that_does_not_give_the_root_is_not_claimed(self):
        blob = b"".join(reversed(self.ids))
        self.assertIsNone(self._run(self._record(), blob=blob))

    def test_a_list_of_the_wrong_length_is_not_claimed(self):
        self.assertIsNone(self._run(self._record(), blob=b"".join(self.ids[:-1])))

    def test_slot_data_not_held_no_claim(self):
        self.assertIsNone(self._run(self._record(), local=False))

    def test_empty_record_no_claim(self):
        self.assertIsNone(self._run(self._record(new_count=0)))


class TestSplitClaims(unittest.TestCase):
    """Bundles are cut near MAX_CLAIM_TX_BYTES so a tx fits one response page."""

    def _entries(self, count, slot_size):
        return [(*_claim(i, seed=i, slot_size=slot_size), 10) for i in range(count)]

    def _group_bytes(self, group):
        from astreum.expression import NIL, link

        expr = NIL
        for entry in reversed(group):
            expr = link(claim_mod._claim_expr(entry[:4]), expr)
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


class TestBuildClaimsCollectsPayouts(unittest.TestCase):
    """_build_claims_for_records returns (claim, payout) tuples."""

    ERA_SIZE = 1024

    def setUp(self):
        self.secret, self.public = _make_keys()
        self.pk = _public_bytes(self.public)
        self.other_pk = _public_bytes(ed25519.Ed25519PrivateKey.generate().public_key())
        self.storage_id = b"\x0c" * 32
        self.slot_id = b"\x0d" * 32
        self.contract_head = SimpleNamespace(hash=lambda: b"\x0e" * 32)

    def _node(self, winner):
        node = SimpleNamespace(
            config={"chain_id": 1},
            storage_public_key_bytes=winner,
            logger=SimpleNamespace(info=lambda *a, **k: None, exception=lambda *a, **k: None),
        )
        return node

    def _record(self, winner):
        return SimpleNamespace(
            new_size=100,
            last_payment_height=0,
            last_payment_winner=winner,
            last_payment_block_hash=b"\x11" * 32,
            new_count=1,
        )

    def _run(self, node, winner, height):
        latest_block = SimpleNamespace(
            height=height,
            accounts=_AccountsStub(account=SimpleNamespace(data=None)),
        )
        with patch.object(
            claim_mod, "iter_records_in_cold_storage", return_value=iter([self.storage_id])
        ), patch.object(claim_mod, "get_from_radix_tree", return_value=self.contract_head), patch.object(
            claim_mod.StorageRecord, "from_storage", return_value=self._record(winner)
        ), patch.object(
            claim_mod,
            "_compute_pow_and_challenge",
            return_value=(self.storage_id, self.slot_id, [b"\x01" * 32], 7),
        ):
            return _build_claims_for_records(node, latest_block, {})

    def test_wall_drop_branch_payout(self):
        # Non-incumbent takeover: needs 5+ eras elapsed.
        node = self._node(self.other_pk)
        entries = self._run(node, self.other_pk, 6 * self.ERA_SIZE)
        self.assertEqual(
            entries, [(self.storage_id, self.slot_id, [b"\x01" * 32], 7, 100 * 6 * self.ERA_SIZE)]
        )

    def test_incumbent_branch_payout(self):
        node = self._node(self.pk)
        entries = self._run(node, self.pk, 2 * self.ERA_SIZE)
        self.assertEqual(
            entries, [(self.storage_id, self.slot_id, [b"\x01" * 32], 7, 100 * 2 * self.ERA_SIZE)]
        )

    def test_young_record_skipped(self):
        # Non-incumbent with < 5 eras elapsed: no claim.
        node = self._node(self.pk)
        entries = self._run(node, self.other_pk, 2 * self.ERA_SIZE)
        self.assertEqual(entries, [])


if __name__ == "__main__":
    unittest.main()
