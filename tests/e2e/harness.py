"""Shared helpers for the e2e network scenarios.

Every node runs as threads inside the test process, on localhost UDP, with the
real 2 s block slots. ``Network`` owns the nodes and the temp folders, and
stops everything on exit, even when a test fails.
"""

from __future__ import annotations

import socket
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.communication.disconnect import disconnect_node  # noqa: E402
from astreum.communication.node import connect_node  # noqa: E402
from astreum.consensus.account import create_account  # noqa: E402
from astreum.consensus.block.create import create_block  # noqa: E402
from astreum.consensus.block.encoding.decode import get_block_from_storage  # noqa: E402
from astreum.consensus.constants import STORAGE_ADDRESS, TREASURY_ADDRESS  # noqa: E402
from astreum.consensus.models.accounts import Accounts  # noqa: E402
from astreum.consensus.models.receipt import Receipt  # noqa: E402
from astreum.consensus.transaction import create_transaction  # noqa: E402
from astreum.consensus.transaction.code import TransactionCode  # noqa: E402
from astreum.consensus.transaction.treasury.record import TreasuryUserRecord  # noqa: E402
from astreum.consensus.validation import node as validation_node  # noqa: E402
from astreum.consensus.validation.node import validate_blockchain  # noqa: E402
from astreum.consensus.verification.node import verify_blockchain  # noqa: E402
from astreum.expression import ZERO32, resolve_inner_exprs  # noqa: E402
from astreum.node import Node  # noqa: E402
from astreum.storage.exprs import put_expr_in_hot_storage  # noqa: E402
from astreum.storage.radix import RadixTree, get_from_radix_tree, put_in_radix_tree  # noqa: E402

# ``block_spacing`` is 2 s (validation/node.py), but measured block time is
# about 3 s: the worker sleeps in 0.5 s steps and timestamps are whole seconds.
# Timeouts count in these 3 s slots.
SLOT = 3.0
STAKE = 8


def free_port() -> int:
    """Return a UDP port that is free on 127.0.0.1 right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def new_key() -> Ed25519PrivateKey:
    """Return a new Ed25519 private key."""
    return Ed25519PrivateKey.generate()


def public(key: Ed25519PrivateKey) -> bytes:
    """Return the 32-byte public key of ``key``."""
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def address(node: Node) -> str:
    """Return the ``host:port`` string other nodes use to reach ``node``."""
    return f"127.0.0.1:{node.config.port}"


def height(node: Node) -> int:
    """Return the height of the node head, or -1 when it has no head object."""
    block = node.latest_block
    return -1 if block is None else int(block.height)


def status(node: Node) -> str:
    """Return a one-line summary of the node, for failure messages."""
    block = node.latest_block
    head = node.latest_block_hash
    return (
        f"height={'-' if block is None else block.height} "
        f"head={head.hex()[:8] if head else '-'} peers={len(node.peers)}"
    )


def wait_until(predicate, timeout: float, message: str, nodes=(), interval: float = 0.1):
    """Poll ``predicate`` until it returns a truthy value, then return that value.

    Args:
        predicate: A function with no arguments.
        timeout: Seconds to wait.
        message: What the test waited for. Used in the failure text.
        nodes: Nodes (or a name -> node dict) whose status goes in the failure text.
        interval: Seconds between polls.

    Raises:
        AssertionError: When the predicate stays falsy until the timeout.
    """
    deadline = time.time() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.time() >= deadline:
            break
        time.sleep(interval)
    items = nodes.items() if isinstance(nodes, dict) else enumerate(nodes)
    detail = "; ".join(f"{name}: {status(n)}" for name, n in items)
    raise AssertionError(f"timeout after {timeout:.0f}s waiting for {message}. {detail}")


# --------------------------------------------------------------------- genesis


def genesis_with(
    *,
    stakes: dict[bytes, int],
    funds: dict[bytes, int] | None = None,
    start_height: int = 0,
    timestamp: int | None = None,
    customize=None,
):
    """Build a replacement for ``create_genesis_block``.

    The block does not depend on the node that builds it, so two nodes patched
    with the same function build the same genesis hash.

    Args:
        stakes: Public key -> stake in the treasury trie.
        funds: Address -> balance of a plain account.
        start_height: Height of the first block. Genesis is detected by
            ``previous_block_hash == ZERO32``, not by height.
        timestamp: Block timestamp. Fixed at call time when ``None``.
        customize: Optional ``(node, accounts, storage_account)`` hook. It may
            add records to the storage trie and cells to ``accounts.pending_exprs``.

    Returns:
        A function ``(node, validator_public_key, chain_id=0) -> Block``.
    """
    ts = int(time.time()) if timestamp is None else int(timestamp)
    stake_items = list(stakes.items())
    funds = dict(funds or {})
    first_validator = stake_items[0][0]

    def build(node, validator_public_key, chain_id=0):
        accounts = Accounts()
        stake_trie = RadixTree()
        for key, amount in stake_items:
            record = TreasuryUserRecord(balance=amount)
            put_in_radix_tree(stake_trie, node, key, record.expr())
            accounts.pending_exprs.extend(resolve_inner_exprs(node, record.expr())[0])
        total = sum(amount for _, amount in stake_items)
        stake_root = stake_trie.root_hash or ZERO32

        treasury = create_account(balance=total, data_hash=stake_root, counter=0)
        treasury.data = stake_trie
        treasury.data_hash = stake_root
        storage = create_account(balance=0, data_hash=ZERO32, counter=0)
        if customize is not None:
            customize(node, accounts, storage)

        accounts.set_account(TREASURY_ADDRESS, treasury)
        accounts.set_account(STORAGE_ADDRESS, storage)
        for key, _ in stake_items:
            accounts.set_account(key, create_account(balance=0, data_hash=ZERO32, counter=0))
        for addr, balance in funds.items():
            accounts.set_account(addr, create_account(balance=balance))

        root = accounts.update_trie(node)
        for expr in accounts.pending_exprs:
            put_expr_in_hot_storage(node, expr)

        return create_block(
            chain_id=chain_id,
            previous_block_hash=ZERO32,
            previous_block=None,
            height=start_height,
            timestamp=ts,
            accounts_hash=root,
            total_transaction_fee=0,
            total_storage_fee=0,
            statistics=[(1, total, 0, 0)],
            transactions_hash=ZERO32,
            receipts_hash=ZERO32,
            difficulty=0,
            validator_public_key_bytes=first_validator,
            nonce=0,
            signature=b"",
            accounts=accounts,
            transactions=[],
            receipts=[],
        )

    return build


# --------------------------------------------------------------------- network


class Network:
    """Owns the nodes and temp folders of one test. Use as a context manager."""

    def __init__(self) -> None:
        self.nodes: list[Node] = []
        self._dirs: list[tempfile.TemporaryDirectory] = []

    def __enter__(self) -> "Network":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        for node in reversed(self.nodes):
            self.stop(node)
        for directory in self._dirs:
            directory.cleanup()
        self.nodes.clear()
        self._dirs.clear()

    @staticmethod
    def stop(node: Node) -> None:
        """Disconnect ``node`` and wait for its validation worker to end."""
        try:
            disconnect_node(node)
        finally:
            for name in ("consensus_validation_thread", "verify_thread"):
                thread = getattr(node, name, None)
                if thread is not None and thread.is_alive():
                    thread.join(timeout=30)

    def node(
        self, *, seed: Node | None = None, trusted: bool = False, connect: bool = True, **config
    ) -> Node:
        """Create a connected node.

        Args:
            seed: A node to use as the single bootstrap peer.
            trusted: When true, ``seed`` is the ``default_seed`` (head provider).
                When false, ``seed`` goes in ``additional_seeds``.
            connect: When false, the node is not connected. ``validate_blockchain``
                connects it.
            **config: Extra config keys. They override the defaults.
        """
        directory = tempfile.TemporaryDirectory(prefix="astreum_e2e_")
        self._dirs.append(directory)
        cfg = {
            "port": free_port(),
            "default_seed": None,
            "additional_seeds": [],
            "cold_storage_path": directory.name,
            "cold_storage_scale": "KB",
            "storage_index_interval": 1,
            "atom_fetch_interval": 1,
            "peer_timeout_interval": 1,
            "verify_blockchain_interval": 0.5,
            "logging_enabled": False,
            "verbose": False,
        }
        if seed is not None:
            if trusted:
                cfg["default_seed"] = address(seed)
            else:
                cfg["additional_seeds"] = [address(seed)]
        cfg.update(config)
        node = Node(config=cfg)
        node._e2e_dir = directory
        self.nodes.append(node)
        if connect:
            connect_node(node)
        return node

    def validator(self, key: Ed25519PrivateKey, *, genesis=None, **kwargs) -> Node:
        """Create a node and start validation with ``key``.

        Args:
            key: The validator private key.
            genesis: A function from ``genesis_with``. The default genesis is
                used when ``None``.
            **kwargs: Passed to ``node``.
        """
        node = self.node(connect=False, **kwargs)
        self.start_validation(node, key, genesis=genesis)
        return node

    @staticmethod
    def start_validation(node: Node, key: Ed25519PrivateKey, *, genesis=None) -> None:
        if genesis is None:
            validate_blockchain(node, key)
            return
        with mock.patch.object(validation_node, "create_genesis_block", genesis):
            validate_blockchain(node, key)

    def user(self, seed: Node, genesis, **kwargs) -> Node:
        """Create a temporary user node that follows ``seed``'s chain.

        A plain non-validator node cannot hold the head block or know the
        validators (see the ``expectedFailure`` tests), so a user here is a
        validator-capable node with no stake. It uses the same ``genesis``
        function as the validator, so it starts from the same genesis hash.
        """
        return self.validator(new_key(), genesis=genesis, seed=seed, **kwargs)

    def peer(self, seed: Node, *, trusted: bool = False, **kwargs) -> Node:
        """Create a non-validator node that follows ``seed``.

        A trusted peer takes the head hash from its seed and does not verify.
        A verifying peer checks the chain and picks the head itself.
        """
        node = self.node(seed=seed, trusted=trusted, **kwargs)
        if not trusted:
            verify_blockchain(node)
        return node


# ------------------------------------------------------------------ sending


def push_transaction(node: Node, tx) -> int:
    """Send ``tx`` from ``node`` to every validator it knows, over the network.

    This is the second half of ``send_transaction`` (consensus/transaction/send.py).
    ``send_transaction`` itself cannot be used: it looks for validators in
    ``validation_route.peers``, which nothing fills.

    Returns:
        The number of validators the message was queued for.
    """
    from astreum.communication.models.message import Message, MessageTopic
    from astreum.communication.outgoing_queue import enqueue_outgoing
    from astreum.consensus.transaction.message import encode_transaction_message
    from astreum.consensus.validation.peers import validator_peers

    tx_exprs, missed = resolve_inner_exprs(node, tx.expr())
    if missed:
        raise RuntimeError("transaction data unavailable locally")
    for expr in tx_exprs:
        put_expr_in_hot_storage(node, expr)
    payload = encode_transaction_message(tx_exprs)
    queued = 0
    for peer in validator_peers(node).values():
        message = Message(
            topic=MessageTopic.TRANSACTION,
            content=payload,
            sender_public_key_bytes=node.storage_public_key_bytes,
        )
        message.encrypt(peer.shared_key_bytes)
        if enqueue_outgoing(node, peer.address, message=message, difficulty=peer.difficulty):
            queued += 1
    return queued


def knows_validator(node: Node) -> bool:
    """Return true when ``node`` has at least one validator peer with an address."""
    from astreum.consensus.validation.peers import validator_peers

    return bool(validator_peers(node))


# --------------------------------------------------------------------- reading


def chain(node: Node) -> list:
    """Return the blocks from genesis to the node head, linked by ``previous_block``."""
    blocks = []
    block_hash = node.latest_block_hash
    while True:
        block = get_block_from_storage(astreum_node=node, block_hash=block_hash)
        blocks.append(block)
        if block.previous_block_hash == ZERO32:
            break
        block_hash = block.previous_block_hash
    blocks.reverse()
    for previous, block in zip(blocks, blocks[1:]):
        block.previous_block = previous
    return blocks


def block_at(node: Node, at: int):
    """Return the block at chain height ``at`` by walking back from the head."""
    for block in reversed(chain(node)):
        if block.height == at:
            return block
    return None


def assert_chain_links(test, node: Node, signers: set[bytes] | None = None) -> list:
    """Check heights, hash links and timestamps of the whole chain. Return it."""
    blocks = chain(node)
    test.assertGreaterEqual(len(blocks), 1)
    for previous, block in zip(blocks, blocks[1:]):
        test.assertEqual(block.height, previous.height + 1)
        test.assertEqual(block.previous_block_hash, previous.expr_id)
        test.assertGreater(block.timestamp, previous.timestamp)
        if signers is not None:
            test.assertIn(block.validator_public_key_bytes, signers)
    return blocks


def account(node: Node, addr: bytes, block=None):
    """Return the account at ``addr`` in ``block`` (default: the head), or ``None``."""
    block = block or node.latest_block
    return Accounts(root_hash=block.accounts_hash, block_hash=block.expr_id).get_account(addr, node)


def balance(node: Node, addr: bytes, block=None) -> int:
    """Return the balance of ``addr``. A missing account counts as 0."""
    found = account(node, addr, block)
    return 0 if found is None else found.balance


def transfer(node: Node, key: Ed25519PrivateKey, counter: int, to: bytes, amount: int, **kw):
    """Build a signed TRANSFER transaction with its hash cached."""
    tx = create_transaction(
        chain_id=node.config.chain_id,
        sender=public(key),
        counter=counter,
        recipient=to,
        code=TransactionCode.TRANSFER,
        amount=amount,
        secret_key=key,
        **kw,
    )
    tx.hash = tx.expr().hash()
    return tx


def tx_hashes(node: Node, block) -> list[bytes]:
    """Return the transaction hashes of ``block`` in order."""
    from astreum.expression.helpers import get_expr_builtin_tag
    from astreum.storage.exprs import get_expr_list

    if block.transactions_hash == ZERO32:
        return []
    cell, hashes = get_expr_list(node, block.transactions_hash), []
    while cell is not None and get_expr_builtin_tag(cell) == "link":
        # A long list ends in a link cell with no head. It is not a transaction.
        if cell.head_hash is not None:
            hashes.append(cell.head_hash)
        cell = cell.tail
    return hashes


def find_tx_block(node: Node, tx_hash: bytes):
    """Return the block that holds ``tx_hash``, or ``None``. Scans the whole chain."""
    for block in chain(node):
        if tx_hash in tx_hashes(node, block):
            return block
    return None


def receipt(node: Node, block, tx_hash: bytes) -> Receipt | None:
    """Return the receipt of ``tx_hash`` stored in ``block``."""
    trie = RadixTree(root_hash=block.receipts_hash, root_block=block.expr_id)
    head = get_from_radix_tree(trie, node, tx_hash)
    if head is None:
        return None
    return Receipt.from_storage(node, head.hash())
