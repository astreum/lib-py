import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src")

from astreum.expression import (
    RESOLUTION_FULL,
    RESOLUTION_RECORD,
    RESOLUTION_SINGLE,
    ZERO32,
    int_,
    link,
    NIL,
)
from astreum.storage.exprs import network as net

H = [bytes([i]) * 32 for i in range(1, 6)]


def _node(connected=True):
    return SimpleNamespace(is_connected=connected, logger=MagicMock())


class _Stop(Exception):
    pass


class TestPrefetchHelper(unittest.TestCase):
    def test_fetches_the_missing_hashes_in_one_call(self):
        node = _node()
        local = {H[0]: int_(1)}
        with patch.object(net, "get_expr_from_local_storage", side_effect=lambda n, h: local.get(h)), \
             patch.object(net, "get_exprs_from_network") as fetch:
            net.prefetch_exprs_from_network(node, H[:3], RESOLUTION_SINGLE)
        fetch.assert_called_once_with(node, [(H[1], RESOLUTION_SINGLE), (H[2], RESOLUTION_SINGLE)])

    def test_nothing_missing_sends_nothing(self):
        node = _node()
        with patch.object(net, "get_expr_from_local_storage", return_value=int_(1)), \
             patch.object(net, "get_exprs_from_network") as fetch:
            net.prefetch_exprs_from_network(node, H[:3], RESOLUTION_SINGLE)
        fetch.assert_not_called()

    def test_duplicates_and_zero_hash_are_skipped(self):
        node = _node()
        with patch.object(net, "get_expr_from_local_storage", return_value=None), \
             patch.object(net, "get_exprs_from_network") as fetch:
            net.prefetch_exprs_from_network(node, [H[0], H[0], ZERO32, H[1]], RESOLUTION_SINGLE)
        fetch.assert_called_once_with(node, [(H[0], RESOLUTION_SINGLE), (H[1], RESOLUTION_SINGLE)])

    def test_disconnected_node_does_nothing(self):
        with patch.object(net, "get_exprs_from_network") as fetch:
            net.prefetch_exprs_from_network(_node(connected=False), H[:2], RESOLUTION_SINGLE)
        fetch.assert_not_called()

    def test_full_resolution_skips_fully_local_trees_only(self):
        node = _node()
        whole = link(int_(1), int_(2))
        partial = net.Expr("link", head_hash=b"\xaa" * 32, tail_hash=b"\xbb" * 32)
        trees = {H[0]: whole, H[1]: partial}
        with patch("astreum.storage.exprs.full.get_expr_full_from_local_storage",
                   side_effect=lambda n, h: trees.get(h)), \
             patch.object(net, "get_exprs_from_network") as fetch:
            net.prefetch_exprs_from_network(node, [H[0], H[1], H[2]], RESOLUTION_FULL)
        fetch.assert_called_once_with(node, [(H[1], RESOLUTION_FULL), (H[2], RESOLUTION_FULL)])

    def test_never_raises(self):
        node = _node()
        with patch.object(net, "get_expr_from_local_storage", side_effect=RuntimeError("disk")):
            net.prefetch_exprs_from_network(node, H[:2], RESOLUTION_SINGLE)


class TestBloomSearchPrefetch(unittest.TestCase):
    def test_block_txs_are_prefetched_once_before_loading(self):
        from astreum.crypto.bloom_search import search

        tx_list = link(None, link(None, NIL))
        tx_list._head_hash = H[0]
        tx_list._tail._head_hash = H[1]
        block = SimpleNamespace(transactions_hash=b"\x09" * 32)
        order = []

        with patch.object(search, "get_expr_list", return_value=tx_list), \
             patch.object(search, "prefetch_exprs_from_network",
                          side_effect=lambda n, hashes, res: order.append(("prefetch", list(hashes), res))), \
             patch("astreum.consensus.transaction.from_storage.get_transaction_from_storage",
                   side_effect=lambda n, h: order.append(("load", h)) or SimpleNamespace(hash=h)):
            txs = search._load_block_txs(_node(), block)

        self.assertEqual(order[0], ("prefetch", [H[0], H[1]], RESOLUTION_FULL))
        self.assertEqual([o for o in order[1:]], [("load", H[0]), ("load", H[1])])
        self.assertEqual(len(txs), 2)


class TestValidatorPrefetch(unittest.TestCase):
    def test_stake_records_are_prefetched_before_decoding(self):
        from astreum.consensus.validation import validator

        records = {b"A" * 32: int_(1), b"B" * 32: int_(2)}
        treasury = SimpleNamespace(data=object())
        accounts = SimpleNamespace(get_account=lambda address, node: treasury)
        block = SimpleNamespace(timestamp=10, accounts_hash=b"\x01" * 32)

        with patch.object(validator, "get_block_from_storage", return_value=block), \
             patch.object(validator, "Accounts", return_value=accounts), \
             patch.object(validator, "get_all_from_radix_tree", return_value=records), \
             patch.object(validator, "prefetch_exprs_from_network", side_effect=_Stop) as prefetch:
            with self.assertRaises(_Stop):
                validator.current_validator(_node(), b"\x02" * 32)

        node_arg, hashes, resolution = prefetch.call_args.args
        self.assertEqual(hashes, [e.hash() for e in records.values()])
        self.assertEqual(resolution, RESOLUTION_FULL)


class TestPaymentPrefetch(unittest.TestCase):
    """_prefetch_claim_records only fetches for claims the verify loop would process."""

    def _run(self, *, record_ok=True, slot=None, local=None, claims=None, record=None):
        from astreum.consensus.transaction.storage import payment

        storage_id, slot_id = b"\x01" * 32, b"\x02" * 32
        record = record or SimpleNamespace(last_payment_block_hash=b"\x03" * 32, new_count=4)
        claims = claims if claims is not None else [(storage_id, slot_id, 0)]

        from blake3 import blake3
        seed = blake3(record.last_payment_block_hash + storage_id).digest()
        challenge = int.from_bytes(seed[:8], "little") % record.new_count
        slot = (storage_id, challenge) if slot is None else slot

        with patch.object(payment, "get_from_radix_tree",
                          side_effect=lambda tree, n, k: SimpleNamespace(hash=lambda: k)), \
             patch.object(payment.StorageRecord, "from_storage",
                          return_value=record if record_ok else None), \
             patch("astreum.storage.records.parse_slot", return_value=slot), \
             patch("astreum.storage.exprs.get_expr_from_local_storage", return_value=local), \
             patch("astreum.storage.exprs.get_exprs_from_network") as fetch:
            payment._prefetch_claim_records(_node(), SimpleNamespace(data=object()), claims)
        return fetch

    def test_fetches_record_when_slot_data_is_missing(self):
        fetch = self._run()
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[1], [(b"\x01" * 32, RESOLUTION_RECORD)])

    def test_slot_data_already_local_sends_nothing(self):
        self._run(local=int_(1)).assert_not_called()

    def test_missing_record_sends_nothing(self):
        self._run(record_ok=False).assert_not_called()

    def test_slot_for_another_record_sends_nothing(self):
        self._run(slot=(b"\x09" * 32, 0)).assert_not_called()

    def test_wrong_challenge_index_sends_nothing(self):
        self._run(slot=(b"\x01" * 32, 99)).assert_not_called()

    def test_bad_payment_block_hash_sends_nothing(self):
        record = SimpleNamespace(last_payment_block_hash=b"short", new_count=4)
        self._run(record=record).assert_not_called()

    def test_zero_count_record_is_ignored_not_raised(self):
        record = SimpleNamespace(last_payment_block_hash=b"\x03" * 32, new_count=0)
        from astreum.consensus.transaction.storage import payment
        with patch.object(payment, "get_from_radix_tree",
                          side_effect=lambda t, n, k: SimpleNamespace(hash=lambda: k)), \
             patch.object(payment.StorageRecord, "from_storage", return_value=record), \
             patch("astreum.storage.exprs.get_exprs_from_network") as fetch:
            payment._prefetch_claim_records(
                _node(), SimpleNamespace(data=object()), [(b"\x01" * 32, b"\x02" * 32, 0)]
            )
        fetch.assert_not_called()

    def test_many_claims_go_out_in_one_call(self):
        claims = [(bytes([i]) * 32, b"\x02" * 32, 0) for i in (1, 2, 3)]
        from astreum.consensus.transaction.storage import payment
        record = SimpleNamespace(last_payment_block_hash=b"\x03" * 32, new_count=1)
        with patch.object(payment, "get_from_radix_tree",
                          side_effect=lambda t, n, k: SimpleNamespace(hash=lambda: k)), \
             patch.object(payment.StorageRecord, "from_storage", return_value=record), \
             patch("astreum.storage.records.parse_slot",
                   side_effect=[(claims[0][0], 0), (claims[1][0], 0), (claims[2][0], 0)]), \
             patch("astreum.storage.exprs.get_expr_from_local_storage", return_value=None), \
             patch("astreum.storage.exprs.get_exprs_from_network") as fetch:
            payment._prefetch_claim_records(_node(), SimpleNamespace(data=object()), claims)
        fetch.assert_called_once()
        self.assertEqual([e[0] for e in fetch.call_args.args[1]], [c[0] for c in claims])


if __name__ == "__main__":
    unittest.main()
