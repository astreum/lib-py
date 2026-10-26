import sys
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
for path in (ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.consensus.validation.genesis import create_genesis_block
from astreum.consensus.validation.validator import current_validator
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.expression import resolve_inner_exprs
from astreum.consensus.models.accounts import extract_accounts_exprs
from astreum.storage.exprs import put_expr_in_hot_storage
from astreum.node import Node
from astreum.consensus.validation import validator as validator_module
from astreum.consensus.block.nonce import generate_block_nonce
from astreum.consensus.block.encoding.expr import get_block_expr
from tests.e2e.harness import genesis_with


class TestNodeValidator(unittest.TestCase):
    def test_current_validator_after_genesis(self) -> None:
        node = Node()
        private_key = Ed25519PrivateKey.generate()
        validator_public_key = private_key.public_key().public_bytes(
            Encoding.Raw,
            PublicFormat.Raw,
        )
        block = create_genesis_block(node, validator_public_key)
        self.assertIsNotNone(block.accounts, "genesis block must expose the accounts trie")

        block.accounts.update_trie(node)

        from astreum.consensus.block.nonce import generate_block_nonce
        generate_block_nonce(block=block, difficulty=1)
        from astreum.consensus.block.encoding.expr import get_block_expr
        block_hash = get_block_expr(block).hash()
        block_exprs, _ = resolve_inner_exprs(node, get_block_expr(block))

        for e in extract_accounts_exprs(block.accounts):
            put_expr_in_hot_storage(node, e)
        for e in block_exprs:
            put_expr_in_hot_storage(node, e)

        validator_key, _ = current_validator(
            node=node,
            block_hash=block_hash,
            target_time=block.timestamp + 1,
        )

        self.assertEqual(validator_key, validator_public_key)


class TestTwoValidators(unittest.TestCase):
    """The draw for a block depends on the time the block is drawn for."""

    TIMESTAMP = 1_000_000

    def setUp(self) -> None:
        self.node = Node()
        self.key_a = b"\x0a" * 32
        self.key_b = b"\x0b" * 32
        block = genesis_with(
            stakes={self.key_a: 1 << 20, self.key_b: 1 << 20},
            timestamp=self.TIMESTAMP,
        )(self.node, self.key_a)
        block.accounts.update_trie(self.node)
        generate_block_nonce(block=block, difficulty=1)
        self.block_hash = get_block_expr(block).hash()
        block_exprs, _ = resolve_inner_exprs(self.node, get_block_expr(block))
        for e in extract_accounts_exprs(block.accounts):
            put_expr_in_hot_storage(self.node, e)
        for e in block_exprs:
            put_expr_in_hot_storage(self.node, e)

    def _draw(self, **kwargs) -> bytes:
        key, _ = current_validator(self.node, self.block_hash, **kwargs)
        return key

    def test_default_time_is_now(self) -> None:
        for slots in range(1, 12):
            now = self.TIMESTAMP + 2 * slots
            with mock.patch.object(validator_module.time, "time", return_value=now):
                drawn = self._draw()
            self.assertEqual(drawn, self._draw(target_time=now), f"slots={slots}")

    def test_times_in_one_slot_draw_the_same_validator(self) -> None:
        self.assertEqual(
            self._draw(target_time=self.TIMESTAMP + 5),
            self._draw(target_time=self.TIMESTAMP + 6),
        )

    def test_later_time_can_draw_the_other_validator(self) -> None:
        first = self._draw(target_time=self.TIMESTAMP + 2)
        drawn = {
            self._draw(target_time=self.TIMESTAMP + 2 * slots) for slots in range(1, 12)
        }
        self.assertEqual(drawn, {self.key_a, self.key_b})
        self.assertIn(first, drawn)


if __name__ == "__main__":
    unittest.main()
