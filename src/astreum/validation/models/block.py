
from typing import Any, List, Optional, Tuple, TYPE_CHECKING

from ...storage.models.atom import Atom, AtomKind, ZERO32, bytes_list_to_atoms
from .accounts import Accounts
from .receipt import Receipt
from .transaction import apply_transaction

if TYPE_CHECKING:
    from ...storage.models.trie import Trie
    from .transaction import Transaction
    from .receipt import Receipt

def _int_to_be_bytes(n: Optional[int]) -> bytes:
    if n is None:
        return b""
    n = int(n)
    if n == 0:
        return b"\x00"
    size = (n.bit_length() + 7) // 8
    return n.to_bytes(size, "big")


def _be_bytes_to_int(b: Optional[bytes]) -> int:
    if not b:
        return 0
    return int.from_bytes(b, "big")


class Block:
    """Validation Block representation using Atom storage.

    Top-level encoding:
      block_id = type_atom.object_id()
      chain: type_atom --next--> version_atom --next--> signature_atom --next--> body_list_atom --next--> ZERO32
      where: type_atom        = Atom(kind=AtomKind.SYMBOL, data=b"block")
             version_atom     = Atom(kind=AtomKind.BYTES,  data=b"\x01")
             signature_atom   = Atom(kind=AtomKind.BYTES, data=<signature-bytes>)
             body_list_atom   = Atom(kind=AtomKind.LIST,  data=<body_head_id>)

    Details order in body_list:
      0: chain                               (byte)
      1: previous_block_hash                 (bytes)
      2: number                              (int -> big-endian bytes)
      3: timestamp                           (int -> big-endian bytes)
      4: accounts_hash                       (bytes)
      5: transactions_total_fees             (int -> big-endian bytes)
      6: transactions_hash                   (bytes)
      7: receipts_hash                       (bytes)
      8: delay_difficulty                    (int -> big-endian bytes)
      9: validator_public_key_bytes         (bytes)
      10: nonce                              (int -> big-endian bytes)

    Notes:
      - "body tree" is represented here by the body_list id (self.body_hash), not
        embedded again as a field to avoid circular references.
      - "signature" is a field on the class but is not required for validation
        navigation; include it in the instance but it is not encoded in atoms
        unless explicitly provided via details extension in the future.
    """

    # essential identifiers
    version: int
    atom_hash: Optional[bytes]
    chain_id: int
    previous_block_hash: bytes
    previous_block: Optional["Block"]

    # block details
    number: int
    timestamp: Optional[int]
    accounts_hash: Optional[bytes]
    transactions_total_fees: Optional[int]
    transactions_hash: Optional[bytes]
    receipts_hash: Optional[bytes]
    delay_difficulty: Optional[int]
    validator_public_key_bytes: Optional[bytes]
    nonce: Optional[int]

    # additional
    body_hash: Optional[bytes]
    signature: Optional[bytes]

    # structures
    accounts: Optional["Trie"]
    transactions: Optional[List["Transaction"]]
    receipts: Optional[List["Receipt"]]
    
    def __init__(
        self,
        *,
        chain_id: int,
        previous_block_hash: bytes,
        previous_block: Optional["Block"],
        number: int,
        timestamp: Optional[int],
        accounts_hash: Optional[bytes],
        transactions_total_fees: Optional[int],
        transactions_hash: Optional[bytes],
        receipts_hash: Optional[bytes],
        delay_difficulty: Optional[int],
        validator_public_key_bytes: Optional[bytes],
        version: int = 1,
        nonce: Optional[int] = None,
        signature: Optional[bytes] = None,
        atom_hash: Optional[bytes] = None,
        body_hash: Optional[bytes] = None,
        accounts: Optional["Trie"] = None,
        transactions: Optional[List["Transaction"]] = None,
        receipts: Optional[List["Receipt"]] = None,
    ) -> None:
        self.version = int(version)
        self.atom_hash = atom_hash
        self.chain_id = chain_id
        self.previous_block_hash = previous_block_hash
        self.previous_block = previous_block
        self.number = number
        self.timestamp = timestamp
        self.accounts_hash = accounts_hash
        self.transactions_total_fees = transactions_total_fees
        self.transactions_hash = transactions_hash
        self.receipts_hash = receipts_hash
        self.delay_difficulty = delay_difficulty
        self.validator_public_key_bytes = (
            bytes(validator_public_key_bytes) if validator_public_key_bytes else None
        )
        self.nonce = nonce
        self.body_hash = body_hash
        self.signature = signature
        self.accounts = accounts
        self.transactions = transactions
        self.receipts = receipts

    def to_atom(self) -> Tuple[bytes, List[Atom]]:
        # Build body details as direct byte atoms, in defined order
        detail_payloads: List[bytes] = []
        block_atoms: List[Atom] = []

        def _emit(detail_bytes: bytes) -> None:
            detail_payloads.append(detail_bytes)

        # 0: chain
        _emit(_int_to_be_bytes(self.chain_id))
        # 1: previous_block_hash
        _emit(self.previous_block_hash)
        # 2: number
        _emit(_int_to_be_bytes(self.number))
        # 3: timestamp
        _emit(_int_to_be_bytes(self.timestamp))
        # 4: accounts_hash
        _emit(self.accounts_hash or b"")
        # 5: transactions_total_fees
        _emit(_int_to_be_bytes(self.transactions_total_fees))
        # 6: transactions_hash
        _emit(self.transactions_hash or b"")
        # 7: receipts_hash
        _emit(self.receipts_hash or b"")
        # 8: delay_difficulty
        _emit(_int_to_be_bytes(self.delay_difficulty))
        # 9: validator_public_key_bytes
        _emit(self.validator_public_key_bytes or b"")
        # 10: nonce
        _emit(_int_to_be_bytes(self.nonce))

        # Build body list chain directly from detail atoms
        body_head = ZERO32
        detail_atoms: List[Atom] = []
        for payload in reversed(detail_payloads):
            atom = Atom(data=payload, next_id=body_head, kind=AtomKind.BYTES)
            detail_atoms.append(atom)
            body_head = atom.object_id()
        detail_atoms.reverse()

        block_atoms.extend(detail_atoms)

        body_list_atom = Atom(data=body_head, kind=AtomKind.LIST)
        self.body_hash = body_list_atom.object_id()

        # Signature atom links to body list atom; type atom links to signature atom
        sig_atom = Atom(
            data=bytes(self.signature or b""),
            next_id=self.body_hash,
            kind=AtomKind.BYTES,
        )
        version_atom = Atom(
            data=_int_to_be_bytes(self.version),
            next_id=sig_atom.object_id(),
            kind=AtomKind.BYTES,
        )
        type_atom = Atom(
            data=b"block",
            next_id=version_atom.object_id(),
            kind=AtomKind.SYMBOL,
        )

        block_atoms.append(body_list_atom)
        block_atoms.append(sig_atom)
        block_atoms.append(version_atom)
        block_atoms.append(type_atom)

        self.atom_hash = type_atom.object_id()
        return self.atom_hash, block_atoms

    @classmethod
    def from_atom(cls, node: Any, block_id: bytes) -> "Block":

        block_header = node.get_atom_list_from_storage(block_id)
        if block_header is None or len(block_header) != 4:
            raise ValueError("malformed block atom chain")
        type_atom, version_atom, sig_atom, body_list_atom = block_header

        if type_atom.kind is not AtomKind.SYMBOL or type_atom.data != b"block":
            raise ValueError("not a block (type atom payload)")
        if version_atom.kind is not AtomKind.BYTES:
            raise ValueError("malformed block (version atom kind)")
        version = _be_bytes_to_int(version_atom.data)
        if version != 1:
            raise ValueError("unsupported block version")
        if sig_atom.kind is not AtomKind.BYTES:
            raise ValueError("malformed block (signature atom kind)")
        if body_list_atom.kind is not AtomKind.LIST:
            raise ValueError("malformed block (body list atom kind)")
        if body_list_atom.next_id != ZERO32:
            raise ValueError("malformed block (body list tail)")

        detail_atoms = node.get_atom_list_from_storage(body_list_atom.data)
        if detail_atoms is None:
            raise ValueError("missing block body list nodes")

        if len(detail_atoms) != 11:
            raise ValueError("block body must contain exactly 11 detail entries")

        detail_values: List[bytes] = []
        for detail_atom in detail_atoms:
            if detail_atom.kind is not AtomKind.BYTES:
                raise ValueError("block body detail atoms must be bytes")
            detail_values.append(detail_atom.data)

        (
            chain_bytes,
            prev_bytes,
            number_bytes,
            timestamp_bytes,
            accounts_bytes,
            fees_bytes,
            transactions_bytes,
            receipts_bytes,
            delay_diff_bytes,
            validator_bytes,
            nonce_bytes,
        ) = detail_values

        return cls(
            version=version,
            chain_id=_be_bytes_to_int(chain_bytes),
            previous_block_hash=prev_bytes or ZERO32,
            previous_block=None,
            number=_be_bytes_to_int(number_bytes),
            timestamp=_be_bytes_to_int(timestamp_bytes),
            accounts_hash=accounts_bytes or None,
            transactions_total_fees=_be_bytes_to_int(fees_bytes),
            transactions_hash=transactions_bytes or None,
            receipts_hash=receipts_bytes or None,
            delay_difficulty=_be_bytes_to_int(delay_diff_bytes),
            validator_public_key_bytes=validator_bytes or None,
            nonce=_be_bytes_to_int(nonce_bytes),
            signature=sig_atom.data if sig_atom is not None else None,
            atom_hash=block_id,
            body_hash=body_list_atom.object_id(),
        )

    def verify(self, node: Any) -> bool:
        """Verify receipts, transactions, and accounts hashes for this block."""
        if node is None:
            raise ValueError("node required for block verification")

        logger = getattr(node, "logger", None)

        def _hex(value: Optional[bytes]) -> str:
            if isinstance(value, (bytes, bytearray)):
                return value.hex()
            return str(value)

        def _log_debug(message: str, *args: object) -> None:
            if logger:
                logger.debug(message, *args)

        def _log_warning(message: str, *args: object) -> None:
            if logger:
                logger.warning(message, *args)

        _log_debug("Block verify start block=%s", _hex(self.atom_hash))

        if self.transactions_hash is None:
            _log_warning("Block verify missing transactions_hash block=%s", _hex(self.atom_hash))
            return False
        if self.receipts_hash is None:
            _log_warning("Block verify missing receipts_hash block=%s", _hex(self.atom_hash))
            return False
        if self.accounts_hash is None:
            _log_warning("Block verify missing accounts_hash block=%s", _hex(self.atom_hash))
            return False

        def _load_hash_list(head: bytes) -> Optional[List[bytes]]:
            if head == ZERO32:
                return []
            atoms = node.get_atom_list_from_storage(head)
            if atoms is None:
                _log_warning("Block verify missing list atoms head=%s block=%s", _hex(head), _hex(self.atom_hash))
                return None
            for atom in atoms:
                if atom.kind is not AtomKind.BYTES:
                    _log_warning("Block verify list atom kind mismatch head=%s block=%s", _hex(head), _hex(self.atom_hash))
                    return None
            return [bytes(atom.data) for atom in atoms]

        prev_hash = self.previous_block_hash or ZERO32
        if prev_hash == ZERO32:
            if self.transactions_hash != ZERO32:
                _log_warning("Block verify genesis tx hash mismatch block=%s", _hex(self.atom_hash))
                return False
            if self.receipts_hash != ZERO32:
                _log_warning("Block verify genesis receipts hash mismatch block=%s", _hex(self.atom_hash))
                return False
            if self.transactions_total_fees not in (0, None):
                _log_warning("Block verify genesis fees mismatch block=%s", _hex(self.atom_hash))
                return False
            _log_debug("Block verify genesis passed block=%s", _hex(self.atom_hash))
            return True

        try:
            prev_block = self.previous_block or Block.from_atom(node, prev_hash)
        except Exception:
            _log_warning("Block verify failed loading parent block=%s prev=%s", _hex(self.atom_hash), _hex(prev_hash))
            return False

        prev_accounts_hash = getattr(prev_block, "accounts_hash", None)
        if not prev_accounts_hash:
            _log_warning("Block verify missing parent accounts hash block=%s", _hex(self.atom_hash))
            return False

        tx_hashes = _load_hash_list(self.transactions_hash)
        if tx_hashes is None:
            _log_warning("Block verify failed loading tx list block=%s", _hex(self.atom_hash))
            return False

        expected_tx_head, _ = bytes_list_to_atoms(tx_hashes)
        if expected_tx_head != (self.transactions_hash or ZERO32):
            _log_warning(
                "Block verify tx head mismatch block=%s expected=%s actual=%s",
                _hex(self.atom_hash),
                _hex(expected_tx_head),
                _hex(self.transactions_hash),
            )
            return False

        accounts_snapshot = Accounts(root_hash=prev_accounts_hash)
        work_block = type("_WorkBlock", (), {})()
        work_block.chain_id = self.chain_id
        work_block.accounts = accounts_snapshot
        work_block.transactions = []
        work_block.receipts = []

        total_fees = 0
        for tx_hash in tx_hashes:
            try:
                total_fees += apply_transaction(node, work_block, tx_hash)
            except Exception:
                _log_warning(
                    "Block verify failed applying tx=%s block=%s",
                    _hex(tx_hash),
                    _hex(self.atom_hash),
                )
                return False

        if self.transactions_total_fees is None:
            _log_warning("Block verify missing total fees block=%s", _hex(self.atom_hash))
            return False
        if int(self.transactions_total_fees) != int(total_fees):
            _log_warning(
                "Block verify fees mismatch block=%s expected=%s actual=%s",
                _hex(self.atom_hash),
                total_fees,
                self.transactions_total_fees,
            )
            return False

        applied_transactions = list(work_block.transactions or [])
        if len(applied_transactions) != len(tx_hashes):
            _log_warning(
                "Block verify tx count mismatch block=%s expected=%s actual=%s",
                _hex(self.atom_hash),
                len(tx_hashes),
                len(applied_transactions),
            )
            return False

        expected_receipts: List[Receipt] = list(work_block.receipts or [])
        if len(expected_receipts) != len(applied_transactions):
            _log_warning(
                "Block verify receipt count mismatch block=%s expected=%s actual=%s",
                _hex(self.atom_hash),
                len(applied_transactions),
                len(expected_receipts),
            )
            return False
        expected_receipt_ids: List[bytes] = []
        for receipt in expected_receipts:
            receipt_id, _ = receipt.to_atom()
            expected_receipt_ids.append(receipt_id)

        expected_receipts_head, _ = bytes_list_to_atoms(expected_receipt_ids)
        if expected_receipts_head != (self.receipts_hash or ZERO32):
            _log_warning(
                "Block verify receipts head mismatch block=%s expected=%s actual=%s",
                _hex(self.atom_hash),
                _hex(expected_receipts_head),
                _hex(self.receipts_hash),
            )
            return False

        stored_receipt_ids = _load_hash_list(self.receipts_hash)
        if stored_receipt_ids is None:
            _log_warning("Block verify failed loading receipts list block=%s", _hex(self.atom_hash))
            return False
        if stored_receipt_ids != expected_receipt_ids:
            _log_warning("Block verify receipts list mismatch block=%s", _hex(self.atom_hash))
            return False
        for expected, stored_id in zip(expected_receipts, stored_receipt_ids):
            try:
                stored = Receipt.from_atom(node, stored_id)
            except Exception:
                _log_warning(
                    "Block verify failed loading receipt=%s block=%s",
                    _hex(stored_id),
                    _hex(self.atom_hash),
                )
                return False
            if stored.transaction_hash != expected.transaction_hash:
                _log_warning(
                    "Block verify receipt tx mismatch receipt=%s block=%s",
                    _hex(stored_id),
                    _hex(self.atom_hash),
                )
                return False
            if stored.status != expected.status:
                _log_warning(
                    "Block verify receipt status mismatch receipt=%s block=%s",
                    _hex(stored_id),
                    _hex(self.atom_hash),
                )
                return False
            if stored.cost != expected.cost:
                _log_warning(
                    "Block verify receipt cost mismatch receipt=%s block=%s",
                    _hex(stored_id),
                    _hex(self.atom_hash),
                )
                return False
            if stored.logs_hash != expected.logs_hash:
                _log_warning(
                    "Block verify receipt logs hash mismatch receipt=%s block=%s",
                    _hex(stored_id),
                    _hex(self.atom_hash),
                )
                return False

        try:
            accounts_snapshot.update_trie(node)
        except Exception:
            _log_warning("Block verify failed updating accounts trie block=%s", _hex(self.atom_hash))
            return False
        if accounts_snapshot.root_hash != self.accounts_hash:
            _log_warning(
                "Block verify accounts hash mismatch block=%s expected=%s actual=%s",
                _hex(self.atom_hash),
                _hex(accounts_snapshot.root_hash),
                _hex(self.accounts_hash),
            )
            return False

        _log_debug("Block verify success block=%s", _hex(self.atom_hash))
        return True

    @staticmethod
    def _leading_zero_bits(buf: bytes) -> int:
        """Return the number of leading zero bits in the provided buffer."""
        zeros = 0
        for byte in buf:
            if byte == 0:
                zeros += 8
                continue
            zeros += 8 - int(byte).bit_length()
            break
        return zeros

    @staticmethod
    def calculate_delay_difficulty(
        *,
        previous_timestamp: Optional[int],
        current_timestamp: Optional[int],
        previous_difficulty: Optional[int],
        target_spacing: int = 2,
    ) -> int:
        """
        Adjust the delay difficulty with linear steps relative to block spacing.

        If blocks arrive too quickly (spacing <= 1), difficulty increases by one.
        If blocks are slower than the target spacing, difficulty decreases by one,
        and otherwise remains unchanged.
        """
        base_difficulty = max(1, int(previous_difficulty or 1))
        if previous_timestamp is None or current_timestamp is None:
            return base_difficulty

        spacing = max(0, int(current_timestamp) - int(previous_timestamp))
        if spacing <= 1:
            return base_difficulty + 1
        if spacing > target_spacing:
            return max(1, base_difficulty - 1)
        return base_difficulty

    def generate_nonce(
        self,
        *,
        difficulty: int,
    ) -> int:
        """
        Find a nonce that yields a block hash with the required leading zero bits.

        The search starts from the current nonce and iterates until the target
        difficulty is met.
        """
        target = max(1, int(difficulty))
        start = int(self.nonce or 0)
        nonce = start
        while True:
            self.nonce = nonce
            block_hash, _ = self.to_atom()
            leading_zeros = self._leading_zero_bits(block_hash)
            if leading_zeros >= target:
                self.atom_hash = block_hash
                return nonce
            nonce += 1
