from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from ...storage.models.atom import Atom, AtomKind, ZERO32
from ...utils.integer import bytes_to_int, int_to_bytes
from .account import Account
from ..genesis import TREASURY_ADDRESS
from .receipt import STATUS_FAILED, Receipt, STATUS_SUCCESS

@dataclass
class Transaction:
    chain_id: int
    amount: int
    counter: int
    data: bytes = b""
    recipient: bytes = b""
    sender: bytes = b""
    signature: bytes = b""
    hash: bytes = ZERO32

    def to_atom(self) -> Tuple[bytes, List[Atom]]:
        """Serialise the transaction, returning (object_id, atoms)."""
        detail_payloads: List[bytes] = []
        acc: List[Atom] = []

        def emit(payload: bytes) -> None:
            detail_payloads.append(payload)

        emit(int_to_bytes(self.chain_id))
        emit(int_to_bytes(self.amount))
        emit(int_to_bytes(self.counter))
        emit(bytes(self.data))
        emit(bytes(self.recipient))
        emit(bytes(self.sender))

        body_head = ZERO32
        detail_atoms: List[Atom] = []
        for payload in reversed(detail_payloads):
            atom = Atom(data=payload, next_id=body_head, kind=AtomKind.BYTES)
            detail_atoms.append(atom)
            body_head = atom.object_id()
        detail_atoms.reverse()
        acc.extend(detail_atoms)

        body_list_atom = Atom(data=body_head, kind=AtomKind.LIST)
        acc.append(body_list_atom)
        body_list_id = body_list_atom.object_id()

        signature_atom = Atom(
            data=bytes(self.signature),
            next_id=body_list_id,
            kind=AtomKind.BYTES,
        )
        type_atom = Atom(
            data=b"transaction",
            next_id=signature_atom.object_id(),
            kind=AtomKind.SYMBOL,
        )

        acc.append(signature_atom)
        acc.append(type_atom)

        self.hash = type_atom.object_id()
        return self.hash, acc

    @classmethod
    def from_atom(
        cls,
        node: Any,
        transaction_id: bytes,
    ) -> Transaction:
        storage_get = getattr(node, "storage_get", None)
        if not callable(storage_get):
            raise NotImplementedError("node does not expose a storage getter")

        def _atom_kind(atom: Optional[Atom]) -> Optional[AtomKind]:
            kind_value = getattr(atom, "kind", None)
            if isinstance(kind_value, AtomKind):
                return kind_value
            if isinstance(kind_value, int):
                try:
                    return AtomKind(kind_value)
                except ValueError:
                    return None
            return None

        def _require_atom(
            atom_id: Optional[bytes],
            context: str,
            expected_kind: Optional[AtomKind] = None,
        ) -> Atom:
            if not atom_id or atom_id == ZERO32:
                raise ValueError(f"missing {context}")
            atom = storage_get(atom_id)
            if atom is None:
                raise ValueError(f"missing {context}")
            if expected_kind is not None:
                kind = _atom_kind(atom)
                if kind is not expected_kind:
                    raise ValueError(f"malformed {context}")
            return atom

        type_atom = _require_atom(transaction_id, "transaction type atom", AtomKind.SYMBOL)
        if type_atom.data != b"transaction":
            raise ValueError("not a transaction (type atom payload)")

        signature_atom = _require_atom(type_atom.next_id, "transaction signature atom", AtomKind.BYTES)
        body_list_atom = _require_atom(signature_atom.next_id, "transaction body list atom", AtomKind.LIST)
        if body_list_atom.next_id and body_list_atom.next_id != ZERO32:
            raise ValueError("malformed transaction (body list tail)")

        detail_atoms = node.get_atom_list_from_storage(body_list_atom.data)
        if detail_atoms is None:
            raise ValueError("missing transaction body list nodes")
        if len(detail_atoms) != 6:
            raise ValueError("transaction body must contain exactly 6 detail entries")

        detail_values: List[bytes] = []
        for detail_atom in detail_atoms:
            if detail_atom.kind is not AtomKind.BYTES:
                raise ValueError("transaction detail atoms must be bytes")
            detail_values.append(detail_atom.data)

        (
            chain_id_bytes,
            amount_bytes,
            counter_bytes,
            data_bytes,
            recipient_bytes,
            sender_bytes,
        ) = detail_values

        return cls(
            chain_id=bytes_to_int(chain_id_bytes),
            amount=bytes_to_int(amount_bytes),
            counter=bytes_to_int(counter_bytes),
            data=data_bytes,
            recipient=recipient_bytes,
            sender=sender_bytes,
            signature=signature_atom.data,
            hash=bytes(transaction_id),
        )


def apply_transaction(node: Any, block: object, transaction_hash: bytes) -> int:
    """Apply transaction to the candidate block and return the collected fee."""
    transaction = Transaction.from_atom(node, transaction_hash)

    block_chain = getattr(block, "chain_id", None)
    if block_chain is not None and transaction.chain_id != block_chain:
        return 0

    accounts = getattr(block, "accounts", None)
    if accounts is None:
        raise ValueError("block missing accounts snapshot for transaction application")

    sender_account = accounts.get_account(address=transaction.sender, node=node)
    if sender_account is None:
        return 0

    tx_fee = 1
    tx_cost = tx_fee + transaction.amount

    if sender_account.balance < tx_cost:
        low_sender_balance_receipt = Receipt(
            transaction_hash=bytes(transaction_hash),
            cost=0,
            status=STATUS_FAILED,
        )
        low_sender_balance_receipt.to_atom()
        if block.receipts is None:
            block.receipts = []
        block.receipts.append(low_sender_balance_receipt)
        if block.transactions is None:
            block.transactions = []
        block.transactions.append(transaction)
        return 0

    recipient_account = accounts.get_account(address=transaction.recipient, node=node)
    if recipient_account is None:
        recipient_account = Account.create()

    if transaction.recipient == TREASURY_ADDRESS:
        stake_trie = recipient_account.data
        existing_stake = stake_trie.get(node, transaction.sender)
        current_stake = bytes_to_int(existing_stake)
        new_stake = current_stake + transaction.amount
        stake_trie.put(node, transaction.sender, int_to_bytes(new_stake))
        recipient_account.data_hash = stake_trie.root_hash or ZERO32
        recipient_account.balance += transaction.amount
    else:
        recipient_account.balance += transaction.amount

    sender_account.balance -= tx_cost
    accounts.set_account(transaction.sender, sender_account)
    accounts.set_account(transaction.recipient, recipient_account)

    if block.transactions is None:
        block.transactions = []
    block.transactions.append(transaction)

    receipt = Receipt(
        transaction_hash=bytes(transaction_hash),
        cost=tx_fee,
        status=STATUS_SUCCESS,
    )
    receipt.to_atom()
    if block.receipts is None:
        block.receipts = []
    block.receipts.append(receipt)
    return tx_fee
