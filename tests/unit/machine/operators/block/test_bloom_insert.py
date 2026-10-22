import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from blake3 import blake3

from astreum.machine import tokenize, parse
from astreum.machine.main import Machine
from astreum.crypto.bloom_tree import BloomTree
from astreum.crypto.bloom_search.variants import make_search_variants
from astreum.consensus.transaction.bloom.pending import finalize_pending_bloom_inserts
from astreum.consensus.models.receipt import STATUS_SUCCESS, STATUS_FAILED
from astreum.expression.helpers import get_expr_builtin_tag
from astreum.consensus.constants import PROVIDERS_PER_RECORD


def _make_tx(hash_val=None, sender=None, recipient=None):
    tx = type('Tx', (), {
        'hash': hash_val if hash_val is not None else b"\xaa" * 32,
        'sender': sender if sender is not None else b"\xbb" * 32,
        'recipient': recipient if recipient is not None else b"\xcc" * 32,
    })()
    tx.pending_bloom_keys = set()
    tx.pending_bloom_inserts = set()
    return tx


def _make_block(height=100, previous_block=None):
    if previous_block is None:
        prev_stats = (1_000_000, 1_000_000, 0, 0)
        previous_block = type('Prev', (), {
            'statistics': [prev_stats],
            'cumulative_total_fee': prev_stats[0],
            'cumulative_stake': prev_stats[1],
        })()
    block = type('Block', (), {
        'height': height,
        'previous_block': previous_block,
    })()
    block.bloom_tree = BloomTree()
    block.pending_bloom_keys = set()
    return block


def _setup_machine(block=None, tx=None):
    machine = Machine(node=None, meter_limit=10_000_000)
    machine.block = block if block is not None else _make_block()
    machine.tx = tx if tx is not None else _make_tx()
    return machine


def _tagged_err(result):
    return (
        result is not None
        and get_expr_builtin_tag(result) == "link"
        and result.tail is not None
        and get_expr_builtin_tag(result.tail) == "symbol"
        and result.tail.value == "err"
        and result.head is not None
        and get_expr_builtin_tag(result.head) == "str"
    )


def _variant_hashes(tx, key):
    variants = make_search_variants(tx.hash, tx.sender, tx.recipient, key)
    return {blake3(v).digest() for v in variants}


class TestBlockBloomInsertHandler(unittest.TestCase):
    @staticmethod
    def _parse_expr(hex_bytes: bytes):
        key_hex = "0x" + hex_bytes.hex()
        expr, _ = parse(tokenize(f"({key_hex} block.bloom.insert)"))
        return expr

    def test_expr_inserts_four_variant_hashes(self):
        machine = _setup_machine()
        hex_val = b"\x01" + b"\x00" * 31
        expr = self._parse_expr(hex_val)
        key = expr.head.hash()
        expected = _variant_hashes(machine.tx, key)
        machine.run(expr=expr)
        self.assertEqual(machine.tx.pending_bloom_inserts, expected)
        self.assertEqual(machine.tx.pending_bloom_keys, {key})

    def test_expr_does_not_mutate_tree(self):
        machine = _setup_machine()
        hex_val = b"\x01" + b"\x00" * 31
        expr = self._parse_expr(hex_val)
        machine.run(expr=expr)
        self.assertIsNone(machine.block.bloom_tree.root)

    def test_expr_charges_meter_8(self):
        machine = _setup_machine()
        hex_val = b"\x01" + b"\x00" * 31
        expr = self._parse_expr(hex_val)
        machine.run(expr=expr)
        self.assertEqual(machine.meter.storage, 8)

    def test_two_distinct_calls_accumulate(self):
        machine = _setup_machine()
        h1 = b"\x01" + b"\x00" * 31
        h2 = b"\x02" + b"\x00" * 31
        e1 = self._parse_expr(h1)
        e2 = self._parse_expr(h2)
        k1 = e1.head.hash()
        k2 = e2.head.hash()
        machine.run(expr=e1)
        machine.run(expr=e2)
        expected_inserts = _variant_hashes(machine.tx, k1) | _variant_hashes(machine.tx, k2)
        self.assertEqual(machine.tx.pending_bloom_inserts, expected_inserts)
        self.assertEqual(machine.tx.pending_bloom_keys, {k1, k2})
        self.assertEqual(machine.meter.storage, 16)

    def test_duplicate_call_is_noop(self):
        machine = _setup_machine()
        hex_val = b"\x01" + b"\x00" * 31
        expr = self._parse_expr(hex_val)
        machine.run(expr=expr)
        n = len(machine.tx.pending_bloom_inserts)
        machine.run(expr=expr)
        self.assertEqual(len(machine.tx.pending_bloom_inserts), n)
        self.assertEqual(machine.meter.storage, 8)

    def test_key_already_in_block_set_is_noop(self):
        machine = _setup_machine()
        hex_val = b"\x01" + b"\x00" * 31
        expr = self._parse_expr(hex_val)
        machine.block.pending_bloom_keys.add(expr.head.hash())
        machine.run(expr=expr)
        self.assertEqual(machine.tx.pending_bloom_keys, set())
        self.assertEqual(machine.tx.pending_bloom_inserts, set())
        self.assertEqual(machine.meter.storage, 0)


class TestBlockBloomInsertErrors(unittest.TestCase):
    def test_stack_underflow_returns_err(self):
        machine = _setup_machine()
        expr, _ = parse(tokenize("(block.bloom.insert?)"))
        result = machine.run(expr=expr)
        self.assertTrue(_tagged_err(result))
        self.assertIn("stack underflow", result.head.value)

    def test_no_tx_returns_err(self):
        machine = Machine(node=None, meter_limit=10_000_000)
        machine.block = _make_block()
        machine.tx = None
        key = b"\x01" + b"\x00" * 31
        key_hex = "0x" + key.hex()
        expr, _ = parse(tokenize(f"({key_hex} block.bloom.insert?)"))
        result = machine.run(expr=expr)
        self.assertTrue(_tagged_err(result))
        self.assertIn("transaction context not available", result.head.value)


class TestMakeSearchVariants(unittest.TestCase):
    def test_unkeyed_returns_seven(self):
        z = b"\x00" * 32
        result = make_search_variants(z, z, z, z)
        self.assertEqual(len(result), 7)
        for v in result:
            self.assertEqual(len(v), 160)

    def test_keyed_returns_four(self):
        z = b"\x00" * 32
        key = b"\x01" + b"\x00" * 31
        result = make_search_variants(z, z, z, key)
        self.assertEqual(len(result), 4)
        for v in result:
            self.assertEqual(len(v), 160)

    def test_keyed_all_variants_have_tx_hash(self):
        z = b"\x00" * 32
        tx_hash = b"\xaa" * 32
        key = b"\x01" + b"\x00" * 31
        result = make_search_variants(tx_hash, z, z, key)
        for v in result:
            self.assertEqual(v[:32], tx_hash)

    def test_keyed_all_variants_have_key_slot(self):
        z = b"\x00" * 32
        key = b"\x01" + b"\x00" * 31
        result = make_search_variants(z, z, z, key)
        for v in result:
            self.assertEqual(v[96:128], key)


class TestFinalizePendingBloomInserts(unittest.TestCase):
    def test_success_mutates_tree(self):
        tx = _make_tx()
        k1 = b"\x01" + b"\x00" * 31
        k2 = b"\x02" + b"\x00" * 31
        tx.pending_bloom_keys = {k1, k2}
        tx.pending_bloom_inserts = (
            _variant_hashes(tx, k1) | _variant_hashes(tx, k2)
        )
        block = _make_block()
        fee = finalize_pending_bloom_inserts(None, block, tx, STATUS_SUCCESS)
        self.assertIsNotNone(block.bloom_tree.root)
        self.assertEqual(tx.pending_bloom_keys, set())
        self.assertEqual(tx.pending_bloom_inserts, set())
        self.assertIn(k1, block.pending_bloom_keys)
        self.assertIn(k2, block.pending_bloom_keys)
        # 2 keys * 8 bytes per key, for each provider of the record
        self.assertEqual(fee, 2 * 8 * PROVIDERS_PER_RECORD)

    def test_failed_clears_without_mutation(self):
        tx = _make_tx()
        k1 = b"\x01" + b"\x00" * 31
        tx.pending_bloom_keys = {k1}
        tx.pending_bloom_inserts = _variant_hashes(tx, k1)
        block = _make_block()
        fee = finalize_pending_bloom_inserts(None, block, tx, STATUS_FAILED)
        self.assertIsNone(block.bloom_tree.root)
        self.assertEqual(tx.pending_bloom_keys, set())
        self.assertEqual(tx.pending_bloom_inserts, set())
        self.assertEqual(block.pending_bloom_keys, set())
        self.assertEqual(fee, 0)

    def test_success_empty_set_no_op(self):
        tx = _make_tx()
        block = _make_block()
        fee = finalize_pending_bloom_inserts(None, block, tx, STATUS_SUCCESS)
        self.assertIsNone(block.bloom_tree.root)
        self.assertEqual(fee, 0)

    def test_fee_scales_with_set_size(self):
        tx = _make_tx()
        keys = {bytes([i]) + b"\x00" * 31 for i in range(4)}
        tx.pending_bloom_keys = keys
        tx.pending_bloom_inserts = set()
        for k in keys:
            tx.pending_bloom_inserts |= _variant_hashes(tx, k)
        block = _make_block()
        fee = finalize_pending_bloom_inserts(None, block, tx, STATUS_SUCCESS)
        # 4 keys * 8 bytes per key, for each provider of the record
        self.assertEqual(fee, 4 * 8 * PROVIDERS_PER_RECORD)


if __name__ == "__main__":
    unittest.main(verbosity=2)