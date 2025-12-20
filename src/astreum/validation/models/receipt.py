from __future__ import annotations

from typing import Any, List, Optional, Tuple

from ...storage.models.atom import Atom, AtomKind, ZERO32

STATUS_SUCCESS = 0
STATUS_FAILED = 1


def _int_to_be_bytes(value: Optional[int]) -> bytes:
    if value is None:
        return b""
    value = int(value)
    if value == 0:
        return b"\x00"
    size = (value.bit_length() + 7) // 8
    return value.to_bytes(size, "big")


def _be_bytes_to_int(data: Optional[bytes]) -> int:
    if not data:
        return 0
    return int.from_bytes(data, "big")


class Receipt:
    def __init__(
        self,
        transaction_hash: bytes,
        cost: int,
        status: int,
        logs_hash: bytes = ZERO32,
    ) -> None:
        self.transaction_hash = bytes(transaction_hash)
        self.cost = int(cost)
        self.logs_hash = bytes(logs_hash)
        self.status = int(status)
        self.atom_hash = ZERO32
        self.atoms: List[Atom] = []

    def to_atom(self) -> Tuple[bytes, List[Atom]]:
        if self.status not in (STATUS_SUCCESS, STATUS_FAILED):
            raise ValueError("unsupported receipt status")

        detail_specs = [
            (bytes(self.transaction_hash), AtomKind.LIST),
            (_int_to_be_bytes(self.status), AtomKind.BYTES),
            (_int_to_be_bytes(self.cost), AtomKind.BYTES),
            (bytes(self.logs_hash), AtomKind.LIST),
        ]

        detail_atoms: List[Atom] = []
        next_hash = ZERO32
        for payload, kind in reversed(detail_specs):
            atom = Atom(data=payload, next_id=next_hash, kind=kind)
            detail_atoms.append(atom)
            next_hash = atom.object_id()
        detail_atoms.reverse()

        type_atom = Atom(data=b"receipt", next_id=next_hash, kind=AtomKind.SYMBOL)

        atoms = detail_atoms + [type_atom]
        receipt_id = type_atom.object_id()
        return receipt_id, atoms

    @classmethod
    def from_atom(cls, node: Any, receipt_id: bytes) -> Receipt:
        atom_chain = node.get_atom_list_from_storage(receipt_id)
        if atom_chain is None or len(atom_chain) != 5:
            raise ValueError("malformed receipt atom chain")

        type_atom, tx_atom, status_atom, cost_atom, logs_atom = atom_chain
        if type_atom.kind is not AtomKind.SYMBOL or type_atom.data != b"receipt":
            raise ValueError("not a receipt (type atom)")
        if tx_atom.kind is not AtomKind.LIST:
            raise ValueError("receipt transaction hash must be list-kind")
        if status_atom.kind is not AtomKind.BYTES or cost_atom.kind is not AtomKind.BYTES or logs_atom.kind is not AtomKind.LIST:
            raise ValueError("receipt detail atoms must be bytes-kind")

        transaction_hash_bytes = tx_atom.data
        status_bytes = status_atom.data
        cost_bytes = cost_atom.data
        logs_bytes = logs_atom.data

        status_value = _be_bytes_to_int(status_bytes)
        if status_value not in (STATUS_SUCCESS, STATUS_FAILED):
            raise ValueError("unsupported receipt status")

        receipt = cls(
            transaction_hash=transaction_hash_bytes,
            cost=_be_bytes_to_int(cost_bytes),
            logs_hash=logs_bytes,
            status=status_value,
        )
        receipt.atom_hash = bytes(receipt_id)
        receipt.atoms = atom_chain
        return receipt
