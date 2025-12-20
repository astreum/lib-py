from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Tuple

from ...storage.models.atom import Atom, ZERO32, AtomKind
from ...storage.models.trie import Trie
from ...utils.integer import bytes_to_int, int_to_bytes


@dataclass
class Account:
    balance: int
    code_hash: bytes
    counter: int
    data_hash: bytes
    data: Trie
    atom_hash: bytes = ZERO32
    atoms: List[Atom] = field(default_factory=list)

    @classmethod
    def create(cls, balance: int = 0, data_hash: bytes = ZERO32, code_hash: bytes = ZERO32, counter: int = 0) -> "Account":
        account = cls(
            balance=int(balance),
            code_hash=bytes(code_hash),
            counter=int(counter),
            data_hash=bytes(data_hash),
            data=Trie(root_hash=bytes(data_hash)),
        )
        atom_hash, atoms = account.to_atom()
        account.atom_hash = atom_hash
        account.atoms = atoms
        return account

    @classmethod
    def from_atom(cls, node: Any, root_id: bytes) -> "Account":

        account_atoms = node.get_atom_list_from_storage(root_hash=root_id)

        if account_atoms is None or len(account_atoms) != 5:
            raise ValueError("malformed account atom list")

        type_atom, balance_atom, code_atom, counter_atom, data_atom = account_atoms
        
        if type_atom.data != b"account":
            raise ValueError("not an account (type mismatch)")

        account = cls.create(
            balance=bytes_to_int(balance_atom.data),
            data_hash=data_atom.data,
            counter=bytes_to_int(counter_atom.data),
            code_hash=code_atom.data,
        )

        return account

    def to_atom(self) -> Tuple[bytes, List[Atom]]:
        data_atom = Atom(
            data=bytes(self.data_hash),
            kind=AtomKind.LIST,
        )
        counter_atom = Atom(
            data=int_to_bytes(self.counter),
            next_id=data_atom.object_id(),
            kind=AtomKind.BYTES,
        )
        code_atom = Atom(
            data=bytes(self.code_hash),
            next_id=counter_atom.object_id(),
            kind=AtomKind.LIST,
        )
        balance_atom = Atom(
            data=int_to_bytes(self.balance),
            next_id=code_atom.object_id(),
            kind=AtomKind.BYTES,
        )
        type_atom = Atom(
            data=b"account",
            next_id=balance_atom.object_id(),
            kind=AtomKind.SYMBOL,
        )

        atoms = [data_atom, counter_atom, code_atom, balance_atom, type_atom]
        return type_atom.object_id(), list(atoms)
