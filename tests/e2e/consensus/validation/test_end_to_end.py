"""The block-records release, end to end, on a chain built by the real worker.

One validator builds a chain whose first block holds a transfer to an existing
account, a transfer to a new account, a call to a code account (``acc.put`` and
``tx.log``), a ``STORAGE_CREATE``, two unpaid ``STORAGE_PAYMENT`` claims (a
wrong slot, a wrong proof) and a paid one. A later block holds one more transfer.

Other nodes then start with nothing and may only ask for ``(block_hash,
expr_id)``: the provider here answers a scoped GET only with a cell the block's
record holds (the records table of the validator), as a real provider does. Any
GET that is not scoped, or that the record would not cover, is recorded.
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from queue import Queue
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
for path in (SRC_DIR, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.communication.disconnect import disconnect_node  # noqa: E402
from astreum.communication.node import connect_node  # noqa: E402
from astreum.consensus.account import create_account  # noqa: E402
from astreum.consensus.block.create import create_block  # noqa: E402
from astreum.consensus.block.encoding.decode import get_block_from_storage  # noqa: E402
from astreum.consensus.block.verify import transactions as verify_transactions  # noqa: E402
from astreum.consensus.block.verify.transactions import verify_block_transactions  # noqa: E402
from astreum.consensus.constants import STORAGE_ADDRESS, TREASURY_ADDRESS  # noqa: E402
from astreum.consensus.models.accounts import Accounts  # noqa: E402
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS, Receipt  # noqa: E402
from astreum.consensus.transaction import create_transaction  # noqa: E402
from astreum.consensus.transaction.code import TransactionCode  # noqa: E402
from astreum.consensus.transaction.storage.model import (  # noqa: E402
    StorageLane,
    StorageRecord,
    storage_record_cells,
    storage_record_from_storage,
    storage_record_to_expr,
)
from astreum.consensus.transaction.storage.payment import challenge_index  # noqa: E402
from astreum.consensus.transaction.treasury.record import TreasuryUserRecord  # noqa: E402
from astreum.consensus.validation import node as validation_node  # noqa: E402
from astreum.consensus.validation.node import validate_blockchain  # noqa: E402
from astreum.crypto.bloom_search import bloom_search_tx  # noqa: E402
from astreum.crypto.merkle import create_merkle_tree, generate_merkle_tree_proof  # noqa: E402
from astreum.expression import NIL, ZERO32, bytes_, resolve_inner_exprs  # noqa: E402
from astreum.expression.encoding import decode_expr_from_bytes, encode_expr_to_bytes  # noqa: E402
from astreum.expression.helpers import bytes_list_to_expr  # noqa: E402
from astreum.expression import int_, link, link_list_to_expr  # noqa: E402
from astreum.machine.parser import parse  # noqa: E402
from astreum.machine.tokenizer import tokenize  # noqa: E402
from astreum.node import Node  # noqa: E402
from astreum.consensus.transaction.storage.initial import _typed_payload_size  # noqa: E402
from astreum.expression.expr import get_expr_head_hash  # noqa: E402
from astreum.storage.exprs import get_expr_from_local_storage, put_expr_in_hot_storage  # noqa: E402
from astreum.storage.radix import (  # noqa: E402
    RadixTree,
    get_all_from_radix_tree,
    get_from_radix_tree,
    get_from_radix_tree_with_block,
    put_in_radix_tree,
)
from astreum.storage.records import fetch_and_store_record, get_record_from_cold_storage  # noqa: E402
from tests.e2e.consensus.validation.test_verify_stake_halving import _free_port  # noqa: E402
from astreum.expression.helpers import get_expr_builtin_tag

STAKE = 8
SEED_ID = b"\x5e" * 32  # a record the genesis storage trie already holds
SEED_SLOTS = [bytes_(bytes([i]) * 24) for i in range(5)]
SEED_SIZE = 100
CODE_KEY = b"\xde\xad\xbe\xef"
CODE_VALUE = b"\xca\xfe\xba\xbe"
PROGRAM = "(drop 0xdeadbeef 0xcafebabe acc.put 'hello tx.log)"
MIN_HEIGHT = 6


def _public(key) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _slot_ids(blob):
    chunks = (blob[i : i + 32] for i in range(0, len(blob), 32))
    return [chunk for chunk in chunks if chunk != ZERO32]


def _claim(storage_id, slots, index, nonce=0, lane=0):
    tree = create_merkle_tree([s.hash() for s in slots])
    return link(
        bytes_(storage_id),
        link(
            bytes_(encode_expr_to_bytes(slots[index])),
            link(
                bytes_list_to_expr(generate_merkle_tree_proof(tree, index)),
                link(int_(nonce), link(int_(lane), NIL)),
            ),
        ),
    )


def _claims(*claims):
    payload = NIL
    for claim in reversed(claims):
        payload = link(claim, payload)
    return payload


class Fixture:
    """The genesis accounts and the transactions of the first block."""

    def __init__(self):
        self.validator_secret = Ed25519PrivateKey.generate()
        self.validator = _public(self.validator_secret)
        self.sender_secret = Ed25519PrivateKey.generate()
        self.sender = _public(self.sender_secret)
        self.friend = os.urandom(32)
        self.newcomer = os.urandom(32)
        self.code_account = os.urandom(32)
        program, _ = parse(tokenize(PROGRAM))
        self.program = program
        self.code_hash = program.hash()

    def genesis(self, node, validator_public_key, chain_id=0):
        accounts = Accounts()

        stake_trie = RadixTree()
        stake_record = TreasuryUserRecord(balance=STAKE)
        put_in_radix_tree(stake_trie, node, validator_public_key, stake_record.expr())
        treasury = create_account(balance=STAKE, data_hash=stake_trie.root_hash, counter=0)
        treasury.data = stake_trie
        treasury.data_hash = stake_trie.root_hash
        accounts.pending_exprs.extend(resolve_inner_exprs(node, stake_record.expr())[0])

        # A record the chain already holds, lane 0 last paid to the sender: the
        # first claim on a lane by anyone else needs a proof of work no test can do.
        # Lane 1 is unpaid.
        record = StorageRecord(
            creation_block_hash=SEED_ID,
            lanes=(
                StorageLane(block_hash=SEED_ID, height=0, winner=self.sender),
                StorageLane(block_hash=SEED_ID, height=0, winner=ZERO32),
            ),
            new_size=SEED_SIZE,
            new_count=len(SEED_SLOTS),
            slots_root=create_merkle_tree([s.hash() for s in SEED_SLOTS]).root,
        )
        storage = create_account(balance=0, data_hash=ZERO32, counter=0)
        put_in_radix_tree(storage.data, node, SEED_ID, storage_record_to_expr(record))
        storage.data_hash = storage.data.root_hash
        accounts.pending_exprs.extend(storage_record_cells(record))
        self.seed_record = record

        accounts.pending_exprs.extend(resolve_inner_exprs(node, self.program)[0])
        accounts.set_account(TREASURY_ADDRESS, treasury)
        accounts.set_account(STORAGE_ADDRESS, storage)
        accounts.set_account(validator_public_key, create_account(balance=0, data_hash=ZERO32, counter=0))
        accounts.set_account(self.sender, create_account(balance=10_000_000))
        accounts.set_account(self.friend, create_account(balance=5))
        accounts.set_account(self.code_account, create_account(balance=0, code_hash=self.code_hash))
        root = accounts.update_trie(node)
        # The program and the records are cells the node must hold: genesis
        # only persists the tries and the accounts.
        for expr in accounts.pending_exprs:
            put_expr_in_hot_storage(node, expr)

        return create_block(
            chain_id=chain_id,
            previous_block_hash=ZERO32,
            previous_block=None,
            height=0,
            timestamp=int(time.time()),
            accounts_hash=root,
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

    def tx(self, node, counter, recipient, code=TransactionCode.TRANSFER, **kw):
        tx = create_transaction(
            chain_id=node.config.chain_id,
            counter=counter,
            sender=self.sender,
            recipient=recipient,
            code=code,
            secret_key=self.sender_secret,
            **kw,
        )
        # As a transaction read from storage carries it.
        tx.hash = tx.expr().hash()
        return tx

    def first_block(self, node):
        """(name, transaction) in the order they are applied."""
        challenged = challenge_index(SEED_ID, SEED_ID, len(SEED_SLOTS), 0)
        wrong_slot = (challenged + 1) % len(SEED_SLOTS)
        good = _claim(SEED_ID, SEED_SLOTS, challenged)
        wrong_slot_claim = _claim(SEED_ID, SEED_SLOTS, wrong_slot)
        # The unpaid claims come first: they are tried on the record as the
        # chain holds it, and fail on the claim, not on a record the paid one
        # has already rewritten.
        # The challenged slot with the proof of another index.
        tree = create_merkle_tree([s.hash() for s in SEED_SLOTS])
        wrong_proof = link(
            bytes_(SEED_ID),
            link(
                bytes_(encode_expr_to_bytes(SEED_SLOTS[challenged])),
                link(
                    bytes_list_to_expr(generate_merkle_tree_proof(tree, wrong_slot)),
                    link(int_(0), link(int_(0), NIL)),
                ),
            ),
        )

        items = [bytes_(b"stored-alpha"), bytes_(b"stored-beta")]
        for item in items:
            put_expr_in_hot_storage(node, item)
        listing = link_list_to_expr([i.hash() for i in items])
        for cell in resolve_inner_exprs(node, listing)[0]:
            put_expr_in_hot_storage(node, cell)

        return [
            ("transfer", self.tx(node, 0, self.friend, amount=1000)),
            ("new account", self.tx(node, 1, self.newcomer, amount=777)),
            (
                "code call",
                self.tx(
                    node, 2, self.code_account,
                    code=TransactionCode.CODE_ACCOUNT_CALL, cost_limit=2000, data=NIL,
                ),
            ),
            (
                "storage create",
                self.tx(
                    node, 3, STORAGE_ADDRESS,
                    code=TransactionCode.STORAGE_CREATE, expr_list_id=listing.hash(),
                ),
            ),
            (
                "wrong slot",
                self.tx(node, 4, STORAGE_ADDRESS, code=TransactionCode.STORAGE_PAYMENT, data=_claims(wrong_slot_claim)),
            ),
            (
                "wrong proof",
                self.tx(node, 5, STORAGE_ADDRESS, code=TransactionCode.STORAGE_PAYMENT, data=_claims(wrong_proof)),
            ),
            (
                "paid claim",
                self.tx(node, 6, STORAGE_ADDRESS, code=TransactionCode.STORAGE_PAYMENT, data=_claims(good)),
            ),
        ]


class Provider:
    """The validator as a provider answers: a scoped GET, only for a cell the
    block's record holds. Whatever else is asked is recorded and refused."""

    def __init__(self, creator):
        self.creator = creator
        self.served: list[tuple[bytes, bytes]] = []
        self.unscoped: list[bytes] = []
        self.uncovered: list[tuple[bytes, bytes]] = []
        self._records: dict[bytes, frozenset] = {}
        self.remotes: list = []

    def record(self, block_id):
        """The cells a provider of the block answers for: the record's slots,
        and the payload of a typed value (an int), which is part of its cell."""
        if block_id not in self._records:
            blob = get_record_from_cold_storage(self.creator, block_id)
            held = set(_slot_ids(blob)) if blob else set()
            for slot in list(held):
                cell = get_expr_from_local_storage(self.creator, slot)
                if cell is not None and _typed_payload_size(cell, self.creator, None) is not None:
                    held.add(get_expr_head_hash(cell))
            self._records[block_id] = frozenset(held)
        return self._records[block_id]

    def fresh_node(self):
        directory = tempfile.TemporaryDirectory(prefix="astreum_cold_")
        node = Node(
            config={
                "port": _free_port(),
                "default_seed": None,
                "additional_seeds": [],
                "cold_storage_path": directory.name,
                "cold_storage_scale": "KB",
                "logging_enabled": False,
                "verbose": False,
                "storage_fetch_interval": 0,
                "storage_fetch_retries": 1,
            }
        )
        node.is_connected = True
        node._provider_dir = directory
        self.remotes.append(node)
        return node

    def answer(self, node, expr_id, resolution, *, contact=None, preclaimed=False, block_hash=None):
        if block_hash is None:
            self.unscoped.append(expr_id)
            return False
        # The block's own cell is the root of its record.
        if expr_id != block_hash and expr_id not in self.record(block_hash):
            self.uncovered.append((expr_id, block_hash))
            return False
        expr = get_expr_from_local_storage(self.creator, expr_id)
        if expr is None:
            self.uncovered.append((expr_id, block_hash))
            return False
        self.served.append((expr_id, block_hash))
        # As on the wire: one flat cell, children by hash.
        put_expr_in_hot_storage(node, decode_expr_from_bytes(encode_expr_to_bytes(expr)))
        return True

    def __enter__(self):
        self._patch = mock.patch(
            "astreum.storage.workers.requests.queue_storage_get", side_effect=self.answer
        )
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        for node in self.remotes:
            node._provider_dir.cleanup()

    def assert_clean(self, test):
        test.assertEqual(self.unscoped, [], "GETs without a block")
        test.assertEqual(self.uncovered, [], "GETs the block's record does not hold")


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cold_dir = tempfile.TemporaryDirectory(prefix="astreum_cold_")
        cls.fixture = fx = Fixture()
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
            cls.first = fx.first_block(cls.node)
            queue = Queue()
            for _, tx in cls.first:
                queue.put(tx)

            started = time.time()
            with mock.patch.object(validation_node, "create_genesis_block", fx.genesis), \
                 mock.patch.object(validation_node, "Queue", lambda: queue):
                validate_blockchain(cls.node, fx.validator_secret)

            later = threading.Thread(target=cls._send_later_transfer, daemon=True)
            later.start()

            deadline = time.time() + 900
            while time.time() < deadline and cls.node.latest_block.height < MIN_HEIGHT:
                time.sleep(0.5)
            cls.elapsed = time.time() - started
            cls.node._validation_stop_event.set()
            cls.node.consensus_validation_thread.join(timeout=60)
            later.join(timeout=5)
            if cls.node.latest_block.height < 1:
                raise AssertionError("worker did not produce any block")

            chain = []
            block_hash = cls.node.latest_block_hash
            while True:
                block = get_block_from_storage(astreum_node=cls.node, block_hash=block_hash)
                chain.append(block)
                if block.previous_block_hash == ZERO32:
                    break
                block_hash = block.previous_block_hash
            chain.reverse()
            for block, previous in zip(chain[1:], chain):
                block.previous_block = previous
            cls.chain = chain  # chain[0] is the genesis block
        except BaseException:
            disconnect_node(cls.node)
            cls.cold_dir.cleanup()
            raise

    @classmethod
    def _send_later_transfer(cls) -> None:
        """One more transfer, once the first block is behind us."""
        while cls.node.latest_block is None or cls.node.latest_block.height < 2:
            time.sleep(0.2)
            if cls.node._validation_stop_event.is_set():
                return
        accounts = cls.node.latest_block.accounts
        counter = accounts.get_account(cls.fixture.sender, cls.node).counter
        tx = cls.fixture.tx(cls.node, counter, cls.fixture.friend, amount=42)
        cls.later_transfer = tx
        cls.node._validation_transaction_queue.put(tx)

    @classmethod
    def tearDownClass(cls) -> None:
        disconnect_node(cls.node)
        cls.cold_dir.cleanup()

    # ------------------------------------------------------------------ chain

    def test_the_chain_reaches_height_six_in_under_a_minute(self) -> None:
        self.assertGreaterEqual(self.chain[-1].height, MIN_HEIGHT)
        self.assertLess(self.elapsed, 60)

    def test_the_first_block_holds_every_transaction(self) -> None:
        block = self.chain[1]
        self.assertNotEqual(block.transactions_hash, ZERO32)
        tx_list = self.chain[1].transactions_hash
        from astreum.storage.exprs import get_expr_list

        cell, hashes = get_expr_list(self.node, tx_list), []
        while cell is not None and get_expr_builtin_tag(cell) == "link":
            hashes.append(cell.head_hash)
            cell = cell.tail
        self.assertEqual(hashes, [tx.hash for _, tx in self.first])

    def test_receipts_say_which_transactions_were_paid(self) -> None:
        block = self.chain[1]
        trie = RadixTree(root_hash=block.receipts_hash, root_block=block.expr_id)
        statuses = {}
        for name, tx in self.first:
            head = get_from_radix_tree(trie, self.node, tx.hash)
            receipt = Receipt.from_storage(self.node, head.hash())
            self.assertEqual(receipt.transaction_hash, tx.hash, name)
            statuses[name] = receipt.status
        self.assertEqual(
            statuses,
            {
                "transfer": STATUS_SUCCESS,
                "new account": STATUS_SUCCESS,
                "code call": STATUS_SUCCESS,
                "storage create": STATUS_SUCCESS,
                "paid claim": STATUS_SUCCESS,
                "wrong slot": STATUS_FAILED,
                "wrong proof": STATUS_FAILED,
            },
        )

    def test_only_the_right_claim_was_paid(self) -> None:
        fx = self.fixture
        block = self.chain[1]
        storage = Accounts(root_hash=block.accounts_hash, block_hash=block.expr_id).get_account(
            STORAGE_ADDRESS, self.node
        )
        head = get_from_radix_tree(storage.data, self.node, SEED_ID)
        record = storage_record_from_storage(self.node, head.hash())
        self.assertEqual(record.lanes[0].height, 1)
        self.assertEqual(record.lanes[0].winner, fx.sender)
        # The other lane is untouched.
        self.assertEqual(record.lanes[1], fx.seed_record.lanes[1])
        self.assertEqual(record.slots_root, fx.seed_record.slots_root)
        self.assertEqual(record.new_count, len(SEED_SLOTS))

    def test_the_code_account_wrote_a_key_and_logged(self) -> None:
        fx = self.fixture
        head = self.chain[-1]
        account = Accounts(root_hash=head.accounts_hash, block_hash=head.expr_id).get_account(
            fx.code_account, self.node
        )
        self.assertEqual(get_from_radix_tree(account.data, self.node, CODE_KEY).value, CODE_VALUE)
        first = self.chain[1]
        trie = RadixTree(root_hash=first.receipts_hash, root_block=first.expr_id)
        _, code_tx = self.first[2]
        receipt = Receipt.from_storage(
            self.node, get_from_radix_tree(trie, self.node, code_tx.hash).hash()
        )
        self.assertNotEqual(receipt.logs_hash, ZERO32)

    def test_the_new_account_exists(self) -> None:
        fx = self.fixture
        head = self.chain[-1]
        account = Accounts(root_hash=head.accounts_hash, block_hash=head.expr_id).get_account(
            fx.newcomer, self.node
        )
        self.assertEqual(account.balance, 777)

    # ----------------------------------------------------------------- records

    def _storage_trie(self, block):
        account = Accounts(root_hash=block.accounts_hash, block_hash=block.expr_id).get_account(
            STORAGE_ADDRESS, self.node
        )
        return account.data

    def test_the_storage_trie_holds_only_block_records(self) -> None:
        head = self.chain[-1]
        keys = set(get_all_from_radix_tree(self._storage_trie(head), self.node))
        blocks = {b.expr_id for b in self.chain[:-1]}
        self.assertEqual(keys - {SEED_ID}, blocks)

    def test_slots_per_record_stay_flat_over_empty_blocks(self) -> None:
        empty = [
            len(_slot_ids(get_record_from_cold_storage(self.node, b.expr_id)))
            for b in self.chain[:-1]
            if b.height >= 3 and b.transactions_hash == ZERO32
        ]
        self.assertGreaterEqual(len(empty), 2, "no empty blocks to compare")
        self.assertLess(max(empty), 1000, empty)
        self.assertLess(max(empty) - min(empty), 60, empty)

    def test_storage_put_of_an_inner_expr_is_rejected(self) -> None:
        from types import SimpleNamespace

        from astreum.communication.storage_request.code import StorageRequestCode
        from astreum.communication.storage_request.handle import _handle_put
        from astreum.communication.storage_request.model import StorageRequest
        from astreum.storage.admission import is_expr_in_latest_block

        head = self.chain[-1]
        trie = self._storage_trie(head)
        account_trie = Accounts(root_hash=head.accounts_hash, block_hash=head.expr_id)
        inner = (
            list(trie.nodes)[:3]
            + [head.accounts_hash, head.receipts_hash, head.transactions_hash, head.bloom_hash]
            + list(account_trie._trie.nodes)[:3]
            + [tx.hash for _, tx in self.first]
        )
        # A record id is a key of the storage trie; nothing else is.
        self.assertTrue(is_expr_in_latest_block(self.node, head.previous_block_hash))
        for expr_id in inner:
            self.assertFalse(is_expr_in_latest_block(self.node, expr_id), expr_id.hex())

        peer = SimpleNamespace(address=("127.0.0.1", 1))
        request = StorageRequest(
            StorageRequestCode.STORAGE_PUT,
            data=b"\x00" * 70,
            entries=[(expr_id, 4) for expr_id in inner],
        )
        before = dict(self.node.storage_index)
        ok, reason = _handle_put(self.node, peer, request)
        self.assertFalse(ok)
        self.assertIn("no exprs committed", reason)
        self.assertEqual(self.node.storage_index, before)

    def test_a_later_block_holds_another_transfer(self) -> None:
        later = [b for b in self.chain[2:] if b.transactions_hash != ZERO32]
        self.assertTrue(later, "the later transfer was not included")

    # -------------------------------------------------------------- verifying

    def _load(self, node, block):
        loaded = get_block_from_storage(node, block.expr_id)
        loaded.previous_block = get_block_from_storage(node, block.previous_block_hash)
        return loaded

    def test_the_creator_verifies_its_own_blocks_with_transactions(self) -> None:
        for block in self.chain[1:-1]:
            ok, reason = verify_block_transactions(self.node, block)
            self.assertTrue(ok, f"height {block.height}: {reason}")

    def test_a_node_holding_nothing_verifies_the_blocks_with_transactions(self) -> None:
        with Provider(self.node) as provider:
            verifier = provider.fresh_node()
            self.assertEqual(verifier.hot_storage, {})
            for block in self.chain[1:-1]:
                loaded = self._load(verifier, block)
                ok, reason = verify_block_transactions(verifier, loaded)
                self.assertTrue(ok, f"height {block.height}: {reason}")
        provider.assert_clean(self)
        self.assertTrue(provider.served)
        # The transactions' blocks were asked for their own cells.
        self.assertIn(self.chain[1].expr_id, {b for _, b in provider.served})

    def test_creator_verifier_and_provider_derive_the_same_slots(self) -> None:
        real = verify_transactions.generate_initial_storage_record
        head_storage = self._storage_trie(self.chain[-1])
        with Provider(self.node) as provider:
            verifier = provider.fresh_node()
            storing = provider.fresh_node()
            for block in self.chain[1:-1]:
                record_id = block.previous_block_hash
                made = get_record_from_cold_storage(self.node, record_id)  # the creator's
                self.assertIsNotNone(made, f"height {block.height}")
                seen = {}

                def spy(*args, **kwargs):
                    result = real(*args, **kwargs)
                    if result is not None:
                        seen["slots"], seen["root"] = result[1], result[0].slots_root
                    return result

                with mock.patch.object(verify_transactions, "generate_initial_storage_record", spy):
                    loaded = self._load(verifier, block)
                    ok, reason = verify_block_transactions(verifier, loaded)
                self.assertTrue(ok, f"height {block.height}: {reason}")

                # The verifier's list, whole in memory or as it fetched it.
                self.assertEqual(b"".join(seen["slots"]), made, f"verifier, height {block.height}")

                # The root the chain holds for the record.
                held, _ = get_from_radix_tree_with_block(head_storage, self.node, record_id)
                record = storage_record_from_storage(self.node, held.hash())
                self.assertEqual(seen["root"], record.slots_root)
                self.assertEqual(create_merkle_tree(seen["slots"]).root, record.slots_root)
                self.assertEqual(len(seen["slots"]), record.new_count)

                # A provider replays the walk cell by cell and writes the same list.
                self.assertTrue(
                    fetch_and_store_record(storing, record_id, record),
                    f"provider, height {block.height}",
                )
                self.assertEqual(
                    get_record_from_cold_storage(storing, record_id), made, f"provider, height {block.height}"
                )
        provider.assert_clean(self)

    # ------------------------------------------------------------ light reads

    def test_a_node_holding_nothing_finds_a_transaction_and_its_block_gives_it(self) -> None:
        _, code_tx = self.first[2]
        with Provider(self.node) as provider:
            finder = provider.fresh_node()
            start = get_block_from_storage(finder, self.chain[-2].expr_id)
            found = bloom_search_tx(finder, tx_hash=code_tx.hash, starting_block=start, limit=1)
        provider.assert_clean(self)
        self.assertEqual([tx.hash for tx in found], [code_tx.hash])
        self.assertEqual(found[0].block_hash, self.chain[1].expr_id)
        # The transaction came from the block that holds it.
        tx_gets = [b for h, b in provider.served if h == code_tx.hash]
        self.assertEqual(tx_gets, [self.chain[1].expr_id])

    def test_a_node_holding_nothing_reads_old_writes_by_following_refs(self) -> None:
        fx = self.fixture
        reader_block = self.chain[-2]
        with Provider(self.node) as provider:
            reader = provider.fresh_node()
            block = get_block_from_storage(reader, reader_block.expr_id)
            accounts = Accounts(
                root_hash=block.accounts_hash, contact=None, block_hash=block.expr_id
            )
            account = accounts.get_account(fx.code_account, reader)
            self.assertIsNotNone(account)
            value = get_from_radix_tree(account.data, reader, CODE_KEY)
            newcomer = accounts.get_account(fx.newcomer, reader)
        provider.assert_clean(self)
        self.assertEqual(value.value, CODE_VALUE)
        self.assertEqual(newcomer.balance, 777)
        # Written in block 1, read at a later block: its cells came from block 1.
        asked = {b for _, b in provider.served}
        self.assertIn(self.chain[1].expr_id, asked)
        self.assertGreater(reader_block.height, self.chain[1].height + 2)
        self.assertEqual(account.data_block, self.chain[1].expr_id)
