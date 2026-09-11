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


class TestBuildMultiClaimTx(unittest.TestCase):
    def setUp(self):
        self.secret, self.public = _make_keys()
        self.pk = _public_bytes(self.public)
        self.node = _make_node(self.pk, _AccountsStub())
        self.node.storage_secret_key = self.secret

    def test_uses_given_counter(self):
        tx = _build_multi_claim_tx(self.node, [(b"a" * 32, b"b" * 32, 42)], self.secret, 11)
        self.assertEqual(tx.counter, 11)

    def test_signed_and_well_formed(self):
        tx = _build_multi_claim_tx(self.node, [(b"a" * 32, b"b" * 32, 42)], self.secret, 0)
        self.assertEqual(tx.code, TransactionCode.STORAGE_PAYMENT)
        self.assertEqual(tx.sender, self.pk)
        self.assertTrue(tx.signature)
        self.assertEqual(tx.recipient, b"\x00" * 32)

    def test_consecutive_counters(self):
        tx1 = _build_multi_claim_tx(self.node, [(b"a" * 32, b"b" * 32, 1)], self.secret, 3)
        tx2 = _build_multi_claim_tx(self.node, [(b"a" * 32, b"b" * 32, 2)], self.secret, 4)
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
        return [(bytes([i]) * 32, bytes([i + 1]) * 32, i, payout) for i in range(count)]

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
            return_value=(self.storage_id, self.slot_id, 7),
        ):
            return _build_claims_for_records(node, latest_block, {})

    def test_wall_drop_branch_payout(self):
        # Non-incumbent takeover: needs 5+ eras elapsed.
        node = self._node(self.other_pk)
        entries = self._run(node, self.other_pk, 6 * self.ERA_SIZE)
        self.assertEqual(
            entries, [(self.storage_id, self.slot_id, 7, 100 * 6 * self.ERA_SIZE)]
        )

    def test_incumbent_branch_payout(self):
        node = self._node(self.pk)
        entries = self._run(node, self.pk, 2 * self.ERA_SIZE)
        self.assertEqual(
            entries, [(self.storage_id, self.slot_id, 7, 100 * 2 * self.ERA_SIZE)]
        )

    def test_young_record_skipped(self):
        # Non-incumbent with < 5 eras elapsed: no claim.
        node = self._node(self.pk)
        entries = self._run(node, self.other_pk, 2 * self.ERA_SIZE)
        self.assertEqual(entries, [])


if __name__ == "__main__":
    unittest.main()
