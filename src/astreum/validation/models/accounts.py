from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...storage.models.atom import Atom, ZERO32
from ...storage.models.trie import Trie
from .account import Account


class Accounts:
    def __init__(
        self,
        root_hash: Optional[bytes] = None,
    ) -> None:
        self._trie = Trie(root_hash=root_hash)
        self._cache: Dict[bytes, Account] = {}

    @property
    def root_hash(self) -> Optional[bytes]:
        return self._trie.root_hash

    def get_account(self, address: bytes, node: Optional[Any] = None) -> Optional[Account]:
        cached = self._cache.get(address)
        if cached is not None:
            return cached

        if node is None:
            raise ValueError("Accounts requires a node reference for trie access")

        account_id: Optional[bytes] = self._trie.get(node, address)
        if account_id is None:
            return None

        account = Account.from_atom(node, account_id)
        self._cache[address] = account
        return account

    def set_account(self, address: bytes, account: Account) -> None:
        self._cache[address] = account

    def update_trie(self, node: Any) -> List[Atom]:
        """
        Serialise cached accounts, ensure their associated data tries are materialised,
        and return all atoms that must be stored (data tries, account records, and the
        accounts trie nodes themselves).
        """

        def _node_atoms(trie: Trie) -> List[Atom]:
            emitted: List[Atom] = []
            if not trie.nodes:
                return emitted
            for node_hash in sorted(trie.nodes.keys()):
                trie_node = trie.nodes[node_hash]
                head_hash, atoms = trie_node.to_atoms()
                if head_hash != node_hash:
                    continue
                emitted.extend(atoms)
            return emitted

        data_atoms: List[Atom] = []
        account_atoms: List[Atom] = []

        for address, account in self._cache.items():
            account.data_hash = account.data.root_hash or ZERO32
            data_atoms.extend(_node_atoms(account.data))

            account_id, atoms = account.to_atom()
            self._trie.put(node, address, account_id)
            account_atoms.extend(atoms)

        trie_atoms = _node_atoms(self._trie)
        return data_atoms + account_atoms + trie_atoms
