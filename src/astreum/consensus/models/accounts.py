from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from astreum.expression import Expr, ZERO32
from astreum.storage.pointers import read_field
from astreum.storage.radix import (
    RadixTree,
    get_radix_node_expr,
    get_from_radix_tree_with_block,
    get_all_from_radix_tree,
    put_in_radix_tree,
)
from astreum.consensus.account import Account


def compute_total_balance(accounts: "Accounts", node: Any) -> int:
    """Return the sum of balances across all accounts in *accounts*.

    Accounts present in the in-memory cache are used directly. Accounts
    only present in the backing trie are enumerated and parsed to extract
    their balance.

    Args:
        accounts: The Accounts instance to sum.
        node: A node for trie access.

    Returns:
        The total balance as an integer.
    """
    total = 0
    for account in accounts._cache.values():
        total += account.balance

    root_hash = accounts._trie.root_hash
    if root_hash is None or root_hash == ZERO32:
        return total

    from astreum.expression import (
        get_expr_tag,
        get_expr_value,
        resolve_list_exprs,
    )

    cached_addresses = set(accounts._cache)
    for address, account_expr in get_all_from_radix_tree(
        accounts._trie, node
    ).items():
        if address in cached_addresses:
            continue
        nodes, missed = resolve_list_exprs(
            node,
            account_expr,
            contact=accounts.contact,
            block_hash=accounts.block_hash,
        )
        if missed:
            raise ValueError(
                "unable to resolve account expression for supply scan "
                f"(missed={[h.hex()[:8] for h in missed]})"
            )
        if len(nodes) != 5:
            raise ValueError(
                f"invalid account entry length (got={len(nodes)}, expected=5)"
            )
        balance_node = nodes[4]
        if (
            get_expr_tag(balance_node, node, accounts.contact, accounts.block_hash)
            != "int"
        ):
            raise ValueError("account balance is not an int")
        total += get_expr_value(
            balance_node, node, accounts.contact, accounts.block_hash
        )
    return total


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
        # Addresses passed to set_account during the current top-level
        # transaction. The transaction apply step filters it.
        self.involved: set[bytes] = set()
        # Optional callback used to track total supply changes. When set,
        # balance changes recorded through set_account are reported as deltas.
        self._supply_callback: Optional[Callable[[int], None]] = None
        self._supply_balances: Dict[bytes, int] = {}

    @property
    def root_hash(self) -> Optional[bytes]:
        """Get the root hash of the accounts trie.

        Args:
            None.

        Returns:
            The root hash bytes or None.
        """
        return self._trie.root_hash

    def get_account(self, address: bytes, node: Optional[Any] = None) -> Optional[Account]:
        """Get an account by address.

        Args:
            address: The account address.
            node: Optional node for trie access.

        Returns:
            The Account if found, else None.

        Raises:
            ValueError: If node is None.
        """
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
        if self._supply_callback is not None and address not in self._supply_balances:
            self._supply_balances[address] = account.balance
        return account

    def set_account(self, address: bytes, account: Account) -> None:
        """Set an account in the cache.

        Args:
            address: The account address.
            account: The Account to set.

        Returns: None
        """
        self._cache[address] = account
        self.involved.add(address)
        if self._supply_callback is not None:
            old_balance = self._supply_balances.get(address, 0)
            delta = account.balance - old_balance
            if delta != 0:
                self._supply_callback(delta)
            self._supply_balances[address] = account.balance

    def start_supply_tracking(self, callback: Callable[[int], None]) -> None:
        """Start tracking balance changes as supply deltas.

        Existing cached accounts are seeded as baselines so that the first
        ``set_account`` for a cached account reports only the net change,
        not the account's entire balance.

        Args:
            callback: Called with the signed balance change each time
                ``set_account`` records a new balance for an address.

        Returns: None
        """
        self._supply_callback = callback
        self._supply_balances = {
            address: account.balance for address, account in self._cache.items()
        }

    def stop_supply_tracking(self) -> None:
        """Stop tracking balance changes.

        Returns: None
        """
        self._supply_callback = None
        self._supply_balances = {}

    def supply_snapshot(self) -> Dict[bytes, int]:
        """Return a copy of the recorded supply balances.

        Returns:
            A mapping from address to the balance recorded when tracking
            started or when the account was first loaded.
        """
        return dict(self._supply_balances)

    def supply_restore(self, snapshot: Dict[bytes, int]) -> None:
        """Restore the recorded supply balances.

        Args:
            snapshot: The snapshot returned by ``supply_snapshot``.

        Returns: None
        """
        self._supply_balances = snapshot

    def update_trie(self, node: Any) -> Optional[bytes]:
        """Update the trie with cached accounts.

        Args:
            node: The node for trie operations.

        Returns:
            The root hash of the updated trie.
        """
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
    """Extract all expressions from a radix trie.

    Args:
        trie: The radix trie.

    Returns:
        List of expressions from trie nodes.
    """
    exprs: List[Expr] = []
    for node in trie.nodes.values():
        exprs.append(get_radix_node_expr(node))
        val = node.value
        if val is not None and not isinstance(val, bytes):
            exprs.append(val)
    return exprs


def extract_accounts_exprs(accounts: Accounts) -> List[Expr]:
    """Extract all expressions from an Accounts instance.

    Args:
        accounts: The Accounts instance.

    Returns:
        List of all expressions from accounts and tries.
    """
    exprs: List[Expr] = []

    exprs.extend(_trie_nodes_exprs(accounts._trie))

    for acct in accounts._cache.values():
        acct_expr = acct.expr()
        exprs.append(acct_expr)

        exprs.extend(_trie_nodes_exprs(acct.data))
        exprs.extend(_trie_nodes_exprs(acct.channels))

    return exprs
