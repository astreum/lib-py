"""Shared fixtures and builders for the per-tx-type validation suite.

Each test module exercises ``apply_transaction`` directly against a minimal
in-memory ``_FakeNode`` (no P2P, no threads, no network) — the same pattern
established by ``tests/consensus/transaction/test_apply.py``.

Import name is ``astreum`` (the package is installed editable or ``src`` is
put on ``sys.path`` by each test module).
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from astreum.consensus.account import create_account
from astreum.consensus.account.model import Account
from astreum.consensus.transaction.channel.model import Channel
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.model import Transaction
from astreum.storage.radix import get_radix_node_expr
from astreum.storage.radix.node import radix_node_hash
from astreum.consensus.transaction.treasury.record import (
    TreasuryCreditOffer,
    TreasuryLoanRecord,
    TreasuryUserRecord,
)
from astreum.consensus.block.encoding.expr import get_block_expr
from astreum.crypto.bloom_tree import BloomTree
from astreum.expression import (
    Expr,
    NIL,
    ZERO32,
    int_,
    fp64_,
    str_,
    symbol,
    link,
    link_list_to_expr,
    resolve_inner_exprs,
)
from astreum.storage.radix import RadixTree, get_from_radix_tree, put_in_radix_tree
from astreum.consensus.constants import STORAGE_ADDRESS, TREASURY_ADDRESS
from astreum.consensus.models.accounts import Accounts
from astreum.consensus.models.block import Block
from astreum.consensus.block.create import create_block

# Expr concrete types (Expr is a namespace class, not a base class).
_EXPR_TYPES = (Expr,)

# Far-future / past withdrawal windows (8-byte little-endian, as stored).
FAR_FUTURE_WINDOW = 2**62
PAST_WINDOW = (1).to_bytes(8, "little")


class _FakeNode:
    """Minimal in-memory storage node.

    Mirrors the real Node's storage surface used by ``apply_transaction`` and
    its handlers: ``get_expr`` and ``get_expr_list``. ``get_expr_list`` accepts
    either a bytes hash or an already-resolved Expr (consistent with the
    production ``get_expr_list_from_local_storage``).
    """

    def __init__(self):
        self.hot_storage: dict[bytes, Expr] = {}
        self.hot_storage_lock = threading.Lock()
        self.hot_storage_timestamps: dict[bytes, float] = {}
        self.hot_storage_size = 0
        self.config = {"hot_storage_limit": 10 * 1024 * 1024, "cold_storage_path": None}
        self.is_connected = False
        self.logger = type(
            "FakeLogger",
            (),
            {
                "debug": lambda *a, **kw: None,
                "info": lambda *a, **kw: None,
                "warning": lambda *a, **kw: None,
                "exception": lambda *a, **kw: None,
            },
        )()

    def get_expr(self, expr_id: bytes) -> Optional[Expr]:
        if isinstance(expr_id, _EXPR_TYPES):
            return expr_id
        return self.hot_storage.get(expr_id)

    def get_expr_list(self, root_hash) -> Optional[Expr]:
        if isinstance(root_hash, _EXPR_TYPES):
            return root_hash
        return self.hot_storage.get(root_hash)


# ---------------------------------------------------------------------------
# Expr storage helpers
# ---------------------------------------------------------------------------

def _walk_exprs(expr: Expr) -> list[Expr]:
    """Non-mutating traversal: collect *expr* and all sub-exprs without resolving."""
    result: list[Expr] = []
    visited: set[bytes] = set()

    def _walk(e: Expr) -> None:
        if e is None:
            return
        h = e.hash()
        if h in visited:
            return
        visited.add(h)
        result.append(e)
        if e._tag == "link":
            if e._head is not None:
                _walk(e._head)
            if e._tail is not None:
                _walk(e._tail)

    _walk(expr)
    return result


def store_expr_tree(node: _FakeNode, expr: Expr) -> bytes:
    """Store *expr* and every sub-expr in the node, return the root hash.

    Uses a non-mutating walk so that ``head_hash`` references on hash-only
    Links are preserved (``from_storage`` methods rely on ``head_hash``).
    """
    for e in _walk_exprs(expr):
        node.hot_storage[e.hash()] = e
    return expr.hash()


def seed_expr_list(node: _FakeNode, exprs: list[Expr]) -> bytes:
    """Build a link-list of *exprs*, store it + items, return the list head hash."""
    for e in exprs:
        node.hot_storage[e.hash()] = e
    head = link_list_to_expr([e.hash() for e in exprs])
    store_expr_tree(node, head)
    return head.hash()


def seed_program(node: _FakeNode, program_expr: Expr) -> bytes:
    """Store a program expr tree and return its hash (code_hash)."""
    return store_expr_tree(node, program_expr)


# ---------------------------------------------------------------------------
# Block / account scaffolding
# ---------------------------------------------------------------------------

def make_previous_block(
    *,
    chain_id: int = 1,
    timestamp: int = 0,
    cumulative_fee: int = 1,
    cumulative_stake: int = 1,
    height: int = 0,
    difficulty: int = 1,
) -> Block:
    """Block #0 with controllable cumulative fee and stake in statistics range 0."""
    return create_block(
        chain_id=chain_id,
        previous_block_hash=ZERO32,
        previous_block=None,
        height=height,
        timestamp=timestamp,
        accounts_hash=ZERO32,
        total_transaction_fee=0,
        total_storage_fee=0,
        transactions_hash=ZERO32,
        receipts_hash=ZERO32,
        difficulty=difficulty,
        validator_public_key_bytes=os.urandom(32),
        statistics=[(cumulative_fee, cumulative_stake, 0, 0)],
    )


def make_block(
    node: _FakeNode,
    prev_block: Block,
    *,
    chain_id: int = 1,
    height: int = 1,
) -> Block:
    """Current block extending *prev_block* with empty accounts/txs/receipts."""
    block = create_block(
        chain_id=chain_id,
        previous_block_hash=get_block_expr(prev_block).hash(),
        previous_block=prev_block,
        height=height,
        timestamp=0,
        accounts_hash=ZERO32,
        total_transaction_fee=0,
        total_storage_fee=0,
        transactions_hash=ZERO32,
        receipts_hash=ZERO32,
        difficulty=1,
        validator_public_key_bytes=os.urandom(32),
    )
    block.accounts = Accounts()
    block.transactions = []
    block.receipts = []
    block.bloom_tree = BloomTree()
    return block


def seed_storage_account(block: Block) -> Account:
    """Seed STORAGE_ADDRESS with empty data/channels tries (root_hash=None)."""
    storage_account = Account(
        balance=0,
        code_hash=ZERO32,
        counter=0,
        data_hash=ZERO32,
        channels_hash=ZERO32,
        data=RadixTree(root_hash=None),
        channels=RadixTree(root_hash=None),
    )
    block.accounts.set_account(STORAGE_ADDRESS, storage_account)
    return storage_account


def seed_sender_account(block: Block, balance: int = 10_000_000) -> tuple[bytes, Ed25519PrivateKey]:
    """Create + cache a funded sender account; return (public_key, private_key)."""
    key = Ed25519PrivateKey.generate()
    pk = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    block.accounts.set_account(pk, create_account(balance=balance))
    return pk, key


def seed_account(block: Block, address: bytes, *, balance: int = 0) -> Account:
    """Create + cache an account at *address* with the given balance."""
    acct = create_account(balance=balance)
    block.accounts.set_account(address, acct)
    return acct


# ---------------------------------------------------------------------------
# Transaction helpers
# ---------------------------------------------------------------------------

def store_tx(node: _FakeNode, tx: Transaction) -> bytes:
    """Store a transaction's full expr tree in the node and return its hash."""
    return store_expr_tree(node, tx.expr())


def flush_pending(node: _FakeNode, block: Block) -> None:
    """Store every expr in ``block.pending_exprs`` into hot storage.

    Mirrors what the validation worker does after ``apply_transaction`` so that
    records referenced by trie hashes (channels, treasury records, storage
    records) can be read back within a unit test.
    """
    for expr in block.pending_exprs:
        node.hot_storage[expr.hash()] = expr


# ---------------------------------------------------------------------------
# Channel helpers
# ---------------------------------------------------------------------------

def seed_channel(
    node: _FakeNode,
    account: Account,
    counterparty: bytes,
    *,
    balance: int,
    counter: int,
    withdrawal_window: int,
) -> bytes:
    """Seed a channel from *account* to *counterparty* and return its head hash."""
    ch = Channel(
        balance=balance,
        counter=counter,
        withdrawal_window=withdrawal_window,
    )
    ch_head = store_expr_tree(node, ch.expr())
    put_in_radix_tree(account.channels, node, counterparty, ch_head)
    # Store trie node exprs so fresh RadixTree(root_hash=...) can fetch them.
    for trie_node in account.channels.nodes.values():
        node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)
    account.channels_hash = account.channels.root_hash or ZERO32
    return ch_head


# ---------------------------------------------------------------------------
# Treasury helpers
# ---------------------------------------------------------------------------

def seed_treasury_account(
    node: _FakeNode,
    block: Block,
    *,
    treasury_balance: int,
    user_records: Optional[dict[bytes, TreasuryUserRecord]] = None,
) -> Account:
    """Set up the TREASURY_ADDRESS account with an optional set of user records."""
    treasury = create_account(balance=treasury_balance, data_hash=ZERO32)
    for addr, record in (user_records or {}).items():
        rec_head = store_expr_tree(node, record.expr())
        put_in_radix_tree(treasury.data, node, addr, rec_head)
    # Store trie node exprs so fresh RadixTree(root_hash=...) can fetch them.
    for trie_node in treasury.data.nodes.values():
        node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)
    treasury.data_hash = treasury.data.root_hash or ZERO32
    block.accounts.set_account(TREASURY_ADDRESS, treasury)
    return treasury


def seed_user_with_loan(
    node: _FakeNode,
    treasury_account: Account,
    *,
    sender: bytes,
    user_balance: int,
    loan_tx_id: bytes,
    loan: TreasuryLoanRecord,
) -> tuple[TreasuryUserRecord, RadixTree]:
    """Attach a pre-existing loan to a user record inside the treasury data trie."""
    loan_head = store_expr_tree(node, loan.expr())
    loans_trie = RadixTree()
    put_in_radix_tree(loans_trie, node, loan_tx_id, loan_head)
    # Store trie node exprs so a fresh RadixTree(root_hash=...) can fetch them.
    for trie_node in loans_trie.nodes.values():
        node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)

    user_record = TreasuryUserRecord(
        balance=user_balance,
        loans_root_hash=loans_trie.root_hash or ZERO32,
        total_interest_paid=0,
    )
    rec_head = store_expr_tree(node, user_record.expr())
    put_in_radix_tree(treasury_account.data, node, sender, rec_head)
    # Also store the treasury data trie nodes.
    for trie_node in treasury_account.data.nodes.values():
        node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    return user_record, loans_trie


def seed_seller_with_offer(
    node: _FakeNode,
    treasury_account: Account,
    *,
    seller: bytes,
    offer_transaction_id: bytes,
    offer: TreasuryCreditOffer,
    total_interest_paid: int = 0,
    sold_limit: int = 0,
) -> TreasuryUserRecord:
    """Seed a limit seller's `TreasuryUserRecord` with one posted offer.

    Bypasses `TREASURY_SELL` and writes the offer + record directly, since
    unsecured-borrow tests only care about the offer already existing, not
    about how it got posted (that's covered by `test_treasury_sell.py`).
    """
    offer_head = store_expr_tree(node, offer.expr())
    offers_trie = RadixTree()
    put_in_radix_tree(offers_trie, node, offer_transaction_id, offer_head)
    for trie_node in offers_trie.nodes.values():
        node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)

    seller_record = TreasuryUserRecord(
        offers_root_hash=offers_trie.root_hash or ZERO32,
        total_interest_paid=total_interest_paid,
        sold_limit=sold_limit,
    )
    rec_head = store_expr_tree(node, seller_record.expr())
    put_in_radix_tree(treasury_account.data, node, seller, rec_head)
    for trie_node in treasury_account.data.nodes.values():
        node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)
    treasury_account.data_hash = treasury_account.data.root_hash or ZERO32
    return seller_record


__all__ = [
    "FAR_FUTURE_WINDOW",
    "PAST_WINDOW",
    "_FakeNode",
    "make_previous_block",
    "make_block",
    "seed_storage_account",
    "seed_sender_account",
    "seed_account",
    "store_tx",
    "flush_pending",
    "store_expr_tree",
    "seed_expr_list",
    "seed_program",
    "seed_channel",
    "seed_treasury_account",
    "seed_user_with_loan",
    "seed_seller_with_offer",
]
