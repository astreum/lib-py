import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src")

from astreum.expression import (
    RESOLUTION_FULL,
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
        fetch.assert_called_once_with(node, [(H[1], RESOLUTION_SINGLE), (H[2], RESOLUTION_SINGLE)], contact=None)

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
        fetch.assert_called_once_with(node, [(H[0], RESOLUTION_SINGLE), (H[1], RESOLUTION_SINGLE)], contact=None)

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
        fetch.assert_called_once_with(node, [(H[1], RESOLUTION_FULL), (H[2], RESOLUTION_FULL)], contact=None)

    def test_never_raises(self):
        node = _node()
        with patch.object(net, "get_expr_from_local_storage", side_effect=RuntimeError("disk")):
            net.prefetch_exprs_from_network(node, H[:2], RESOLUTION_SINGLE)


class TestBloomSearchPrefetch(unittest.TestCase):
    def test_block_txs_are_prefetched_once_before_loading(self):
        from astreum.crypto.bloom_search import search

        tx_list = link(None, link(None, NIL))
        tx_list.head_hash = H[0]
        tx_list.tail.head_hash = H[1]
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


    def test_block_txs_are_asked_from_the_blocks_provider(self):
        from astreum.crypto.bloom_search import search

        tx_list = link(None, NIL)
        tx_list.head_hash = H[0]
        block = SimpleNamespace(transactions_hash=b"\x09" * 32, expr_id=b"\xb7" * 32)
        calls = {}

        with patch.object(search, "get_expr_list",
                          side_effect=lambda n, h, **kw: calls.setdefault("list", kw) and tx_list), \
             patch.object(search, "prefetch_exprs_from_network",
                          side_effect=lambda n, hashes, res, **kw: calls.setdefault("prefetch", kw)), \
             patch("astreum.consensus.transaction.from_storage.get_transaction_from_storage",
                   side_effect=lambda n, h, **kw: calls.setdefault("tx", kw) and SimpleNamespace(hash=h)):
            search._load_block_txs(_node(), block)

        scope = {"block_hash": b"\xb7" * 32}
        self.assertEqual(calls, {"list": scope, "prefetch": scope, "tx": scope})


class TestValidatorPrefetch(unittest.TestCase):
    def test_stake_records_are_prefetched_before_decoding(self):
        from astreum.consensus.validation import validator

        records = {b"A" * 32: int_(1), b"B" * 32: int_(2)}
        treasury = SimpleNamespace(data=SimpleNamespace(contact=None))
        accounts = SimpleNamespace(get_account=lambda address, node: treasury)
        block = SimpleNamespace(timestamp=10, accounts_hash=b"\x01" * 32)

        with patch.object(validator, "get_block_from_storage", return_value=block), \
             patch.object(validator, "Accounts", return_value=accounts), \
             patch.object(
                 validator,
                 "get_all_from_radix_tree_with_blocks",
                 return_value={k: (v, None) for k, v in records.items()},
             ), \
             patch.object(validator, "prefetch_exprs_from_network", side_effect=_Stop) as prefetch:
            with self.assertRaises(_Stop):
                validator.current_validator(_node(), b"\x02" * 32)

        node_arg, hashes, resolution = prefetch.call_args.args
        self.assertEqual(hashes, [e.hash() for e in records.values()])
        self.assertEqual(resolution, RESOLUTION_FULL)

    def test_stake_records_are_prefetched_per_block(self):
        from astreum.consensus.validation import validator

        block_a, block_b = b"\xa1" * 32, b"\xb2" * 32
        entries = {
            b"A" * 32: (int_(1), block_a),
            b"B" * 32: (int_(2), block_b),
            b"C" * 32: (int_(3), block_a),
        }
        treasury = SimpleNamespace(data=SimpleNamespace(contact=None))
        accounts = SimpleNamespace(get_account=lambda address, node: treasury)
        block = SimpleNamespace(timestamp=10, accounts_hash=b"\x01" * 32)
        calls = []

        def record(node, hashes, resolution, **kw):
            calls.append((hashes, kw.get("block_hash")))
            if len(calls) == 2:
                raise _Stop

        with patch.object(validator, "get_block_from_storage", return_value=block), \
             patch.object(validator, "Accounts", return_value=accounts), \
             patch.object(validator, "get_all_from_radix_tree_with_blocks", return_value=entries), \
             patch.object(validator, "prefetch_exprs_from_network", side_effect=record):
            with self.assertRaises(_Stop):
                validator.current_validator(_node(), b"\x02" * 32)

        self.assertEqual(
            calls,
            [([int_(1).hash(), int_(3).hash()], block_a), ([int_(2).hash()], block_b)],
        )


if __name__ == "__main__":
    unittest.main()
