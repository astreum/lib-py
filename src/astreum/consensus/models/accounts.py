from __future__ import annotations

from typing import Any, Dict, List, Optional

from astreum.expression import Expr, ZERO32
from astreum.storage.pointers import read_field
from astreum.storage.radix import (
    RadixTree,
    get_radix_node_expr,
    get_from_radix_tree_with_block,
    put_in_radix_tree,
)
from astreum.consensus.account import Account


class Accounts:
    def __init__(
        self,
        root_hash: Optional[bytes] = None,
        contact=None,
        block_hash: Optional[bytes] = None,
    ) -> None:
        # ``contact`` is the peer holding this state when it is uncommitted;
        # the trie and every account loaded from it fetch from there.
        self.contact = contact
        # The block whose header holds ``root_hash``: its provider holds the
        # root, and what a plain hash names below it. A cell older than that
        # block is named by a stored ref, which carries its own block.
        self.block_hash = block_hash
        self._trie = RadixTree(root_hash=root_hash, contact=contact, root_block=block_hash)
        self._cache: Dict[bytes, Account] = {}
        self.pending_exprs: List[Expr] = []

    @property
    def root_hash(self) -> Optional[bytes]:
        return self._trie.root_hash

    def get_account(self, address: bytes, node: Optional[Any] = None) -> Optional[Account]:
        cached = self._cache.get(address)
        if cached is not None:
            return cached

        if node is None:
            raise ValueError("Accounts requires a node reference for trie access")

        account_expr, value_block = get_from_radix_tree_with_block(self._trie, node, address)
        if account_expr is None:
            return None

        from astreum.expression import (
            get_expr_tag,
            get_expr_value,
            resolve_list_exprs,
        )
        from astreum.consensus.account.create import create_account

        nodes, missed = resolve_list_exprs(
            node, account_expr, contact=self.contact, block_hash=value_block
        )
        if missed or len(nodes) != 5:
            return None

        data_node, counter_node, code_node, channels_node, balance_node = nodes

        data = read_field(node, data_node, value_block, self.contact)
        code = read_field(node, code_node, value_block, self.contact)
        channels = read_field(node, channels_node, value_block, self.contact)
        if data is None or code is None or channels is None:
            return None
        if not get_expr_tag(counter_node, node, self.contact, value_block) == "int":
            return None
        if not get_expr_tag(balance_node, node, self.contact, value_block) == "int":
            return None

        account = create_account(
            balance=get_expr_value(balance_node, node, self.contact, value_block),
            data_hash=data[0],
            channels_hash=channels[0],
            counter=get_expr_value(counter_node, node, self.contact, value_block),
            code_hash=code[0],
            contact=self.contact,
            data_block=data[1],
            code_block=code[1],
            channels_block=channels[1],
        )
        account._loaded = account.state_key()
        self._cache[address] = account
        return account

    def set_account(self, address: bytes, account: Account) -> None:
        self._cache[address] = account

    def update_trie(self, node: Any) -> Optional[bytes]:
        for address, account in self._cache.items():
            account.data_hash = account.data.root_hash or ZERO32
            account.data_block = account.data.root_block
            account.channels_hash = account.channels.root_hash or ZERO32
            account.channels_block = account.channels.root_block
            key = account.state_key()
            if key == account._loaded:
                continue  # read, not changed: its leaf stays where it is
            # Rebuilt: the fields may have changed since it was last encoded.
            account._expr = account.to_expr()
            put_in_radix_tree(self._trie, node, address, account._expr)
            account._loaded = key
        return self._trie.root_hash


def _trie_nodes_exprs(trie: RadixTree) -> List[Expr]:
    exprs: List[Expr] = []
    for node in trie.nodes.values():
        exprs.append(get_radix_node_expr(node))
        val = node.value
        if val is not None and not isinstance(val, bytes):
            exprs.append(val)
    return exprs


def extract_accounts_exprs(accounts: Accounts) -> List[Expr]:
    exprs: List[Expr] = []

    exprs.extend(_trie_nodes_exprs(accounts._trie))

    for acct in accounts._cache.values():
        acct_expr = acct.expr()
        exprs.append(acct_expr)

        exprs.extend(_trie_nodes_exprs(acct.data))
        exprs.extend(_trie_nodes_exprs(acct.channels))

    return exprs
