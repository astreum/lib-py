"""Block verification applies the scheduled validator's stake halving.

The creator builds block N+1 on the accounts returned by ``current_validator``
(stake halved). ``verify_block_transactions`` must start from the same
accounts, and reject a block signed by someone other than the scheduled
validator.

The blocks under test are produced by the real validation worker on a genesis
whose stake is high enough to halve several times.

The same chain is then verified by a second node that starts with empty
storage and may only ask one peer (V) for data. Every GET it sends is recorded;
a read that forgot to carry V's contact would show up as a GET without it.
"""

import copy
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.communication.disconnect import disconnect_node  # noqa: E402
from astreum.communication.node import connect_node  # noqa: E402
from astreum.consensus.account import create_account  # noqa: E402
from astreum.consensus.block.create import create_block  # noqa: E402
from astreum.consensus.block.encoding.decode import get_block_from_storage  # noqa: E402
from astreum.consensus.block.verify import transactions as verify_transactions  # noqa: E402
from astreum.consensus.block.verify.head import verify_block_head  # noqa: E402
from astreum.consensus.block.verify.transactions import verify_block_transactions  # noqa: E402
from astreum.consensus.constants import STORAGE_ADDRESS, TREASURY_ADDRESS  # noqa: E402
from astreum.consensus.models.accounts import Accounts  # noqa: E402
from astreum.consensus.transaction.treasury.record import TreasuryUserRecord  # noqa: E402
from astreum.consensus.validation import node as validation_node  # noqa: E402
from astreum.consensus.validation.node import validate_blockchain  # noqa: E402
from astreum.expression import ZERO32, resolve_inner_exprs  # noqa: E402
from astreum.expression.encoding import decode_expr_from_bytes, encode_expr_to_bytes  # noqa: E402
from astreum.crypto.merkle import create_merkle_tree  # noqa: E402
from astreum.storage.records import get_record_from_cold_storage  # noqa: E402
from astreum.storage.exprs import get_expr_from_local_storage, put_expr_in_hot_storage  # noqa: E402
from astreum.node import Node  # noqa: E402
from astreum.storage.radix import RadixTree, put_in_radix_tree  # noqa: E402

STAKE = 8


def _genesis_with_stake(node, validator_public_key, chain_id=0):
    """Same as ``create_genesis_block`` but with a stake of ``STAKE``."""
    stake_trie = RadixTree()
    treasury_record = TreasuryUserRecord(balance=STAKE)
    treasury_record_head = treasury_record.expr()
    put_in_radix_tree(stake_trie, node, validator_public_key, treasury_record_head)
    stake_root = stake_trie.root_hash or ZERO32

    treasury_account = create_account(balance=STAKE, data_hash=stake_root, counter=0)
    treasury_account.data = stake_trie
    treasury_account.data_hash = stake_root
    storage_account = create_account(balance=0, data_hash=ZERO32, counter=0)
    validator_account = create_account(balance=0, data_hash=ZERO32, counter=0)

    accounts = Accounts()
    record_exprs, _ = resolve_inner_exprs(node, treasury_record.expr())
    accounts.pending_exprs.extend(record_exprs)
    accounts.set_account(TREASURY_ADDRESS, treasury_account)
    accounts.set_account(STORAGE_ADDRESS, storage_account)
    accounts.set_account(validator_public_key, validator_account)

    accounts_root = accounts.update_trie(node)
    block = create_block(
        chain_id=chain_id,
        previous_block_hash=ZERO32,
        previous_block=None,
        height=0,
        timestamp=int(time.time()),
        accounts_hash=accounts_root,
        total_transaction_fee=0,
        total_storage_fee=0,
        statistics=[(1, STAKE, 0, 0)],
        transactions_hash=ZERO32,
        receipts_hash=ZERO32,
        difficulty=0,
        validator_public_key_bytes=validator_public_key,
        nonce=0,
        signature=b"",
        accounts=accounts,
        transactions=[],
        receipts=[],
    )
    return block


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestVerifyStakeHalving(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cold_dir = tempfile.TemporaryDirectory(prefix="astreum_cold_")
        secret_key = Ed25519PrivateKey.generate()
        cls.validator_key = secret_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )
        cls.node = Node(
            config={
                "port": _free_port(),
                "default_seed": None,
                "additional_seeds": [],
                "cold_storage_path": cls.cold_dir.name,
                "cold_storage_scale": "KB",
                "storage_index_interval": 1,
                "atom_fetch_interval": 1,
                "logging_enabled": False,
                "verbose": False,
            }
        )
        connect_node(cls.node)
        try:
            with mock.patch.object(
                validation_node, "create_genesis_block", _genesis_with_stake
            ):
                validate_blockchain(cls.node, secret_key)

            deadline = time.time() + 900
            while time.time() < deadline and cls.node.latest_block.height < 2:
                time.sleep(0.5)

            cls.node._validation_stop_event.set()
            cls.node.consensus_validation_thread.join(timeout=60)
            if cls.node.latest_block.height < 1:
                raise AssertionError("worker did not produce any block")

            # Walk back from the head: blocks[0] is height 1, genesis excluded.
            chain = []
            block_hash = cls.node.latest_block_hash
            while True:
                block = get_block_from_storage(
                    astreum_node=cls.node, block_hash=block_hash
                )
                if block.previous_block_hash == ZERO32:
                    break
                chain.append(block)
                block_hash = block.previous_block_hash
            chain.reverse()
            for block in chain:
                block.previous_block = get_block_from_storage(
                    astreum_node=cls.node, block_hash=block.previous_block_hash
                )
            cls.chain = chain
        except BaseException:
            disconnect_node(cls.node)
            cls.cold_dir.cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        disconnect_node(cls.node)
        cls.cold_dir.cleanup()

    def test_worker_built_blocks_verify(self) -> None:
        for block in self.chain:
            ok, reason = verify_block_transactions(self.node, block)
            self.assertTrue(ok, f"height {block.height}: {reason}")

    def test_worker_built_blocks_pass_the_header_check(self) -> None:
        # Signature, proof-of-work and difficulty, as a peer checks a BLOCK.
        for block in self.chain:
            self.assertTrue(verify_block_head(self.node, block), f"height {block.height}")

    def test_fresh_node_verifies_using_only_the_validators_contact(self) -> None:
        contact = (b"\x07" * 32, "10.0.0.7", 7000)
        verifier_dir = tempfile.TemporaryDirectory(prefix="astreum_cold_")
        self.addCleanup(verifier_dir.cleanup)
        verifier = Node(
            config={
                "port": _free_port(),
                "default_seed": None,
                "additional_seeds": [],
                "cold_storage_path": verifier_dir.name,
                "cold_storage_scale": "KB",
                "logging_enabled": False,
                "verbose": False,
                "storage_fetch_interval": 0,
                "storage_fetch_retries": 1,
            }
        )
        verifier.is_connected = True
        self.assertEqual(verifier.hot_storage, {})

        gets: list[tuple[bytes, object]] = []

        def validator_answers(node, expr_id, resolution, *, contact=None, preclaimed=False):
            gets.append((expr_id, contact))
            expr = get_expr_from_local_storage(self.node, expr_id)
            if expr is None:
                return False
            # As on the wire: flat, with hash references.
            put_expr_in_hot_storage(
                verifier, decode_expr_from_bytes(encode_expr_to_bytes(expr))
            )
            return True

        with mock.patch(
            "astreum.storage.workers.requests.queue_storage_get",
            side_effect=validator_answers,
        ):
            for block in self.chain:
                block_hash = block.expr_id
                loaded = get_block_from_storage(verifier, block_hash, contact=contact)
                loaded.previous_block = get_block_from_storage(
                    verifier, block.previous_block_hash, contact=contact
                )
                ok, reason = verify_block_transactions(verifier, loaded)
                self.assertTrue(ok, f"height {block.height}: {reason}")

        self.assertTrue(gets, "the verifier should have had to fetch from V")
        stray = [(h.hex()[:12], c) for h, c in gets if c != contact]
        self.assertEqual(stray, [], "GETs that did not go to V")

    def test_first_block_halved_the_stake(self) -> None:
        # Guards the test itself: the blocks above only exercise the fix if the
        # creator really halved the stake.
        first = self.chain[0]
        accounts = Accounts(root_hash=first.accounts_hash)
        treasury = accounts.get_account(TREASURY_ADDRESS, self.node)
        self.assertEqual(treasury.balance, STAKE // 2)

    def test_unscheduled_validator_is_rejected(self) -> None:
        forged = copy.copy(self.chain[0])
        forged.validator_public_key_bytes = bytes(
            Ed25519PrivateKey.generate().public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw
            )
        )
        ok, reason = verify_block_transactions(self.node, forged)
        self.assertFalse(ok)
        self.assertEqual(reason, "unscheduled validator")

    def test_plain_previous_state_no_longer_matches(self) -> None:
        # Verifying from the un-halved previous accounts (the old behaviour)
        # must fail, proving the halving is what makes verification pass.
        first = self.chain[0]

        def plain(node, prev_hash, contact=None):
            return (
                first.validator_public_key_bytes,
                Accounts(root_hash=first.previous_block.accounts_hash),
            )

        with mock.patch.object(verify_transactions, "current_validator", plain):
            ok, reason = verify_block_transactions(self.node, first)
        self.assertFalse(ok)
        self.assertEqual(reason, "accounts hash mismatch")

    def test_a_different_slots_root_fails_the_block(self) -> None:
        # The record carries slots_root, so a record built with another root
        # gives another storage trie and the block's accounts hash no longer
        # matches. The same walk, same root, is what lets the blocks above pass.
        first = self.chain[0]
        real = verify_transactions.generate_initial_storage_record

        def tampered(*args, **kwargs):
            result = real(*args, **kwargs)
            if result is None:
                return result
            record, slots, found, fee = result
            record.slots_root = bytes(32 - 1) + b"\x01"
            record._expr = None
            return record, slots, found, fee

        with mock.patch.object(
            verify_transactions, "generate_initial_storage_record", tampered
        ):
            ok, reason = verify_block_transactions(self.node, first)
        self.assertFalse(ok)
        self.assertEqual(reason, "accounts hash mismatch")

    def test_the_builder_and_verifier_derive_the_same_slots_root(self) -> None:
        for block in self.chain:
            record_id = block.previous_block_hash
            blob = get_record_from_cold_storage(self.node, record_id)  # the builder's list
            real = verify_transactions.generate_initial_storage_record
            seen = {}

            def spy(*args, **kwargs):
                result = real(*args, **kwargs)
                if result is not None:
                    seen["slots"] = result[1]
                    seen["root"] = result[0].slots_root
                return result

            with mock.patch.object(verify_transactions, "generate_initial_storage_record", spy):
                ok, reason = verify_block_transactions(self.node, block)
            self.assertTrue(ok, reason)
            self.assertEqual(b"".join(seen["slots"]), blob, f"height {block.height}")
            self.assertEqual(seen["root"], create_merkle_tree(seen["slots"]).root)


if __name__ == "__main__":
    unittest.main()
