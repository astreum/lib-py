
from typing import Any, Callable, List, Optional, Tuple, TYPE_CHECKING

from ...storage.models.atom import Atom, AtomKind, ZERO32, hash_bytes

if TYPE_CHECKING:
    from ...storage.models.trie import Trie
    from .transaction import Transaction
    from .receipt import Receipt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature


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
      chain: type_atom --next--> signature_atom --next--> body_list_atom --next--> ZERO32
      where: type_atom        = Atom(kind=AtomKind.SYMBOL, data=b"block")
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
      9: validator_public_key               (bytes)
      10: nonce                              (int -> big-endian bytes)

    Notes:
      - "body tree" is represented here by the body_list id (self.body_hash), not
        embedded again as a field to avoid circular references.
      - "signature" is a field on the class but is not required for validation
        navigation; include it in the instance but it is not encoded in atoms
        unless explicitly provided via details extension in the future.
    """

    # essential identifiers
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
    validator_public_key: Optional[bytes]
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
        validator_public_key: Optional[bytes],
        nonce: Optional[int] = None,
        signature: Optional[bytes] = None,
        atom_hash: Optional[bytes] = None,
        body_hash: Optional[bytes] = None,
        accounts: Optional["Trie"] = None,
        transactions: Optional[List["Transaction"]] = None,
        receipts: Optional[List["Receipt"]] = None,
    ) -> None:
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
        self.validator_public_key = validator_public_key
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
        # 9: validator_public_key
        _emit(self.validator_public_key or b"")
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
        type_atom = Atom(data=b"block", next_id=sig_atom.object_id(), kind=AtomKind.SYMBOL)

        block_atoms.append(body_list_atom)
        block_atoms.append(sig_atom)
        block_atoms.append(type_atom)

        self.atom_hash = type_atom.object_id()
        return self.atom_hash, block_atoms

    @classmethod
    def from_atom(cls, node: Any, block_id: bytes) -> "Block":

        block_header = node.get_atom_list_from_storage(block_id)
        if block_header is None or len(block_header) != 3:
            raise ValueError("malformed block atom chain")
        type_atom, sig_atom, body_list_atom = block_header

        if type_atom.kind is not AtomKind.SYMBOL or type_atom.data != b"block":
            raise ValueError("not a block (type atom payload)")
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
            validator_public_key=validator_bytes or None,
            nonce=_be_bytes_to_int(nonce_bytes),
            signature=sig_atom.data if sig_atom is not None else None,
            atom_hash=block_id,
            body_hash=body_list_atom.object_id(),
        )

    def validate(self, storage_get: Callable[[bytes], Optional[Atom]]) -> bool:
        """Validate this block against storage.

        Checks:
        - Signature: signature must verify over the body list id using the
          validator's public key.
        - Timestamp monotonicity: if previous block exists (not ZERO32), this
          block's timestamp must be >= previous.timestamp + 1.
        """
        # Unverifiable if critical fields are missing
        if not self.body_hash:
            return False
        if not self.signature:
            return False
        if not self.validator_public_key:
            return False
        if self.timestamp is None:
            return False

        # 1) Signature check over body hash
        try:
            pub = Ed25519PublicKey.from_public_bytes(bytes(self.validator_public_key))
            pub.verify(self.signature, self.body_hash)
        except InvalidSignature as e:
            raise ValueError("invalid signature") from e

        # 2) Timestamp monotonicity against previous block
        prev_ts: Optional[int] = None
        prev_hash = self.previous_block_hash or ZERO32

        if self.previous_block is not None:
            prev_ts = int(self.previous_block.timestamp or 0)
            prev_hash = self.previous_block.atom_hash or prev_hash or ZERO32

        if prev_hash and prev_hash != ZERO32 and prev_ts is None:
            # If previous block cannot be loaded, treat as unverifiable, not malicious
            try:
                prev = Block.from_atom(storage_get, prev_hash)
            except Exception:
                return False
            prev_ts = int(prev.timestamp or 0)

        if prev_hash and prev_hash != ZERO32:
            if prev_ts is None:
                return False
            cur_ts = int(self.timestamp or 0)
            if cur_ts < prev_ts + 1:
                raise ValueError("timestamp must be at least prev+1")

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
