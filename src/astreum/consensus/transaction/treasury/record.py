from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

from astreum.expression import Expr, NIL, resolve_list_exprs, link, int_, bytes_, get_expr_tag, get_expr_builtin_tag, get_expr_value
from astreum.expression import ZERO32
from astreum.expression.refs import pointer_item
from astreum.consensus.account.model import ROOT_POINTER_SHAPE
from astreum.expression.shape import HOLD, INT, pair
from astreum.storage.exprs import get_expr_list
from astreum.storage.pointers import read_field
from astreum.storage.radix import get_from_radix_tree_with_block
from astreum.consensus.constants import TREASURY_ADDRESS



BORROW_REQUEST_VERSION = 1
BORROW_REQUEST_SIZE = 18
U64_SIZE = 8


# The record of an account in the treasury data trie: balance, loans root,
# total interest paid, guarantees root, loaned, defaulted, guaranteed. A record
# of 4 fields ends after the guarantees root, and the rules for the fields
# after it find no expr.
USER_RECORD_SHAPE = pair(
    INT,
    pair(
        ROOT_POINTER_SHAPE,
        pair(INT, pair(ROOT_POINTER_SHAPE, pair(INT, pair(INT, pair(INT, HOLD))))),
    ),
)


class LoanType(IntEnum):
    SECURED = 0
    UNSECURED = 1


def _root_item(root_hash: bytes, block: Optional[bytes]) -> Expr:
    """Create a root item expression.

    Args:
        root_hash: The root hash.
        block: Optional block identifier.

    Returns:
        A link expression.
    """
    if root_hash == ZERO32:
        return Expr("link", head_hash=ZERO32)
    return pointer_item(root_hash, block)


@dataclass
class TreasuryUserRecord:
    """Per-account state stored in the treasury account's data trie.

    Keyed by the account's own address.

    Attributes:
        balance: Staked ASTR balance backing this account's secured loans.
        loans_root_hash: Root hash of a `RadixTree` of this account's
            secured/unsecured loans, keyed by the `TREASURY_BORROW`
            transaction hash that created each one. `ZERO32` if empty.
        total_interest_paid: Cumulative interest paid across this account's
            repaid/closed loans.
        guarantees_root_hash: Root hash of a `RadixTree` of this account's
            posted guarantees (`TreasuryGuarantee`), keyed by the
            `TREASURY_GUARANTEE` transaction hash that created each one. `ZERO32`
            if empty.
        loaned: Cumulative unsecured principal (`discounted_amount`) this
            account has borrowed, across all its unsecured loans. Feeds the
            insurance rate `p` (see `treasury/borrow.py`).
        defaulted: Cumulative amount permanently written off across this
            account's own unsecured loans, as a borrower. Never reversed.
            Feeds `p`.
        guaranteed: This account's outstanding exposure as a guarantor:
            the sum of `guarantee.amount` over every guarantee this account has
            sold that is currently backing an open loan, plus the
            permanently-lost share from any loan that defaulted while their
            guarantee backed it. A guarantor's available capacity to guarantee
            further is `total_interest_paid - guaranteed`.
    """

    balance: int = 0
    loans_root_hash: bytes = ZERO32
    total_interest_paid: int = 0
    guarantees_root_hash: bytes = ZERO32
    loaned: int = 0
    defaulted: int = 0
    guaranteed: int = 0
    # The block whose record holds each trie root's cell, when it is older than
    # the block writing this record: the root is written as a stored ref.
    loans_root_block: Optional[bytes] = field(default=None, compare=False)
    guarantees_root_block: Optional[bytes] = field(default=None, compare=False)
    _expr: Optional[Expr] = field(default=None, repr=False, compare=False)

    def to_expr(self) -> Expr:
        """Build (without caching) this record's canonical `Expr` encoding.

        Args:
            None.

        Returns:
            A `link`-list `Expr` of the record's fields in storage order.
        """
        if self._expr is not None:
            return self._expr
        detail: Expr = link(int_(self.guaranteed), NIL)
        detail = link(int_(self.defaulted), detail)
        detail = link(int_(self.loaned), detail)
        detail = link(_root_item(self.guarantees_root_hash, self.guarantees_root_block), detail)
        detail = link(int_(self.total_interest_paid), detail)
        detail = link(_root_item(self.loans_root_hash, self.loans_root_block), detail)
        detail = link(int_(self.balance), detail)
        return detail

    def expr(self) -> Expr:
        """Return this record's `Expr` encoding, computing and caching it on first use.

        Args:
            None.

        Returns:
            The cached (or freshly built) `Expr` from `to_expr`.
        """
        if self._expr is not None:
            return self._expr
        self._expr = self.to_expr()
        return self._expr

    @classmethod
    def from_storage(
        cls, node: Any, head_hash: bytes, contact=None, block_hash=None
    ) -> TreasuryUserRecord | None:
        """Decode a `TreasuryUserRecord` from its stored `Expr` encoding.

        Args:
            node: Storage node used to resolve any hash-only sub-exprs.
            head_hash: The 32-byte hash of the record's `link`-list head, as
                produced by `to_expr`/`expr`.
            block_hash: The block whose record holds the record's cells. Its
                provider is asked for them, and a trie root held by a plain
                hash is in it. A root written as a stored ref names its own.

        Returns:
            The decoded `TreasuryUserRecord`, or `None` if *head_hash* is
            empty/`ZERO32`, unresolvable, or doesn't decode to a known field
            count (4, the pre-unsecured-loan shape, or 7, the current
            shape).
        """
        if not head_hash or head_hash == ZERO32:
            return None
        scope = {} if block_hash is None else {"block_hash": block_hash}
        header = get_expr_list(node, head_hash, contact=contact, **scope)
        if header is None or not get_expr_builtin_tag(header) == "link":
            return None
        nodes, missed = resolve_list_exprs(node, header, contact=contact, **scope)
        if missed:
            return None
        if len(nodes) not in (4, 7):
            return None
        fields = []
        blocks: dict[int, Optional[bytes]] = {}
        for i, n in enumerate(nodes):
            if i in (1, 3):  # the loans and guarantees trie roots
                root = read_field(node, n, block_hash, contact)
                if root is None:
                    return None
                fields.append(root[0])
                blocks[i] = root[1] if root[0] != ZERO32 else None
                continue
            tag = get_expr_tag(n, node, contact, block_hash)
            if tag == "int":
                fields.append(get_expr_value(n, node, contact, block_hash))
            else:
                return None
        if len(fields) not in (4, 7):
            return None
        return cls(
            loans_root_block=blocks.get(1),
            guarantees_root_block=blocks.get(3),
            balance=fields[0],
            loans_root_hash=fields[1],
            total_interest_paid=fields[2],
            guarantees_root_hash=fields[3],
            loaned=fields[4] if len(fields) == 7 else 0,
            defaulted=fields[5] if len(fields) == 7 else 0,
            guaranteed=fields[6] if len(fields) == 7 else 0,
        )


def load_from_tree(cls: Any, node: Any, tree: Any, key: bytes, contact=None):
    """Load the record of type *cls* stored at *key* in *tree*.

    Args:
        cls: The class to load.
        node: The node.
        tree: The tree to read from.
        key: The key to look up.
        contact: Optional contact.

    Returns:
        The loaded record or None.
    """
    head, block = get_from_radix_tree_with_block(tree, node, key)
    return cls.from_storage(node, head or ZERO32, contact=contact, block_hash=block)


def load_user_record(
    node: Any, tree: Any, key: bytes, contact=None
) -> TreasuryUserRecord | None:
    """Return the `TreasuryUserRecord` stored at *key* in *tree*.

    The tree is the treasury account's data trie. Read from the block that holds it.
    """
    return load_from_tree(TreasuryUserRecord, node, tree, key, contact)


def load_loan_record(
    node: Any, tree: Any, key: bytes, contact=None
) -> TreasuryLoanRecord | None:
    """The `TreasuryLoanRecord` stored at *key* in a loans trie."""
    return load_from_tree(TreasuryLoanRecord, node, tree, key, contact)


@dataclass(frozen=True)
class TreasuryBorrowRequest:
    loan_type: LoanType
    payment_interval_blocks: int
    payment_count: int


def _claimed_guarantees_to_expr(claimed_guarantees: list[tuple[bytes, bytes, int]]) -> Expr:
    """Encode a loan's claimed-guarantees list as a nested `Expr` link-list.

    Each entry is a 3-field sub-list: ``[guarantor_address,
    guarantee_transaction_id, guarantee_amount]``, where the two 32-byte ids are `bytes`
    values (ids, not exprs a block record could follow, as are `owner` and
    `claimed_by`) and ``guarantee_amount`` is a plain int.

    Args:
        claimed_guarantees: The `(guarantor_address, guarantee_transaction_id, guarantee_amount)`
            triples to encode, in claim order.

    Returns:
        `NIL` if *claimed_guarantees* is empty, otherwise a `link`-list `Expr`
        of the encoded entries.
    """
    if not claimed_guarantees:
        return NIL
    result: Expr = NIL
    for guarantor, guarantee_transaction_id, guarantee_amount in reversed(claimed_guarantees):
        entry: Expr = link(
            bytes_(guarantor),
            link(bytes_(guarantee_transaction_id), link(int_(guarantee_amount), NIL)),
        )
        result = link(entry, result)
    return result


def _claimed_guarantees_from_expr(
    node: Any, claimed_guarantees_node: Expr, contact=None, block_hash=None
) -> list[tuple[bytes, bytes, int]] | None:
    """Decode a claimed-guarantees `Expr`.

    Args:
        node: Storage node used to resolve any hash-only sub-exprs.
        claimed_guarantees_node: The field's own `Expr`, either `NIL` (empty) or
            a `link`-list of 3-field entries.

    Returns:
        The decoded list of `(guarantor_address, guarantee_transaction_id,
        guarantee_amount)` triples, or `None` if the shape doesn't match.
    """
    if claimed_guarantees_node is NIL:
        return []
    scope = {} if block_hash is None else {"block_hash": block_hash}
    entry_nodes, missed = resolve_list_exprs(node, claimed_guarantees_node, contact=contact, **scope)
    if missed:
        return None
    result: list[tuple[bytes, bytes, int]] = []
    for entry_node in entry_nodes:
        sub_nodes, sub_missed = resolve_list_exprs(node, entry_node, contact=contact, **scope)
        if sub_missed or len(sub_nodes) != 3:
            return None
        guarantor_node, guarantee_node, guarantee_amount_node = sub_nodes
        if get_expr_builtin_tag(guarantor_node) != "bytes" or get_expr_builtin_tag(guarantee_node) != "bytes":
            return None
        if get_expr_tag(guarantee_amount_node, node, contact, block_hash) != "int":
            return None
        result.append(
            (
                guarantor_node.value,
                guarantee_node.value,
                get_expr_value(guarantee_amount_node, node, contact, block_hash),
            )
        )
    return result


@dataclass
class TreasuryLoanRecord:
    """A treasury loan record.

    Attributes:
        claimed_guarantees: `(guarantor_address, guarantee_transaction_id, guarantee_amount)`
            triples this loan claimed at origination, needed at loan-end to
            credit each backing guarantor's `guaranteed` back
            (`treasury/repay.py`/`treasury/close.py`). Empty for `SECURED`
            loans.
        insurance_fee: The calculated fee the Treasury deducted at
            origination. `0` for `SECURED` loans.
        missed_count: Cumulative count of installments permanently written
            off on this loan (`treasury/repay.py`/`treasury/close.py`).
            Always `0` for `SECURED` loans, which have no write-off path.
        owner: Current loan owner (32-byte address). The Treasury initially.
            Applies to both SECURED and UNSECURED loans. Bought loans are
            transferred to the buyer.
    """

    creation_block_number: int
    loan_type: LoanType
    discounted_amount: int
    payment_amount: int
    payment_interval_blocks: int
    next_payment_block_number: int
    payment_count: int
    claimed_guarantees: list[tuple[bytes, bytes, int]] = field(default_factory=list)
    insurance_fee: int = 0
    missed_count: int = 0
    owner: bytes = TREASURY_ADDRESS
    _expr: Optional[Expr] = field(default=None, repr=False, compare=False)

    def to_expr(self) -> Expr:
        """Convert loan record to expression.

        Args:
            None.

        Returns:
            The expression representation.
        """
        if self._expr is not None:
            return self._expr
        detail: Expr = link(bytes_(self.owner), NIL)
        detail = link(int_(self.missed_count), detail)
        detail = link(int_(self.insurance_fee), detail)
        detail = link(_claimed_guarantees_to_expr(self.claimed_guarantees), detail)
        detail = link(int_(self.payment_interval_blocks), detail)
        detail = link(int_(self.payment_amount), detail)
        detail = link(int_(self.next_payment_block_number), detail)
        detail = link(int_(int(self.loan_type)), detail)
        detail = link(int_(self.payment_count), detail)
        detail = link(int_(self.discounted_amount), detail)
        detail = link(int_(self.creation_block_number), detail)
        return detail

    def expr(self) -> Expr:
        """Get cached expression or compute it.

        Args:
            None.

        Returns:
            The expression representation.
        """
        if self._expr is not None:
            return self._expr
        self._expr = self.to_expr()
        return self._expr

    @classmethod
    def from_storage(
        cls, node: Any, head_hash: bytes, contact=None, block_hash=None
    ) -> TreasuryLoanRecord | None:
        if not head_hash or head_hash == ZERO32:
            return None
        scope = {} if block_hash is None else {"block_hash": block_hash}
        header = get_expr_list(node, head_hash, contact=contact, **scope)
        if header is None or not get_expr_builtin_tag(header) == "link":
            return None
        nodes, missed = resolve_list_exprs(node, header, contact=contact, **scope)
        if missed:
            return None
        if len(nodes) != 11:
            return None
        int_fields: list[int] = []
        claimed_guarantees: list[tuple[bytes, bytes, int]] = []
        insurance_fee = 0
        missed_count = 0
        owner = TREASURY_ADDRESS
        for i, n in enumerate(nodes):
            if i == 7:
                decoded = _claimed_guarantees_from_expr(node, n, contact, block_hash)
                if decoded is None:
                    return None
                claimed_guarantees = decoded
                continue
            if i == 10:
                if get_expr_builtin_tag(n) != "bytes":
                    return None
                owner = n.value
                continue
            if get_expr_tag(n, node, contact, block_hash) == "int":
                int_fields.append(get_expr_value(n, node, contact, block_hash))
            else:
                return None
        if len(int_fields) != 9:
            return None
        insurance_fee = int_fields[7]
        missed_count = int_fields[8]
        try:
            loan_type = LoanType(int_fields[3])
        except ValueError:
            return None
        return cls(
            creation_block_number=int_fields[0],
            loan_type=loan_type,
            discounted_amount=int_fields[1],
            payment_count=int_fields[2],
            next_payment_block_number=int_fields[4],
            payment_amount=int_fields[5],
            payment_interval_blocks=int_fields[6],
            claimed_guarantees=claimed_guarantees,
            insurance_fee=insurance_fee,
            missed_count=missed_count,
            owner=owner,
        )


@dataclass
class TreasuryGuarantee:
    """A guarantor's posted, fixed-size, fixed-duration, fixed-price guarantee.

    ``claimed_by`` is ``ZERO32`` while available (stored as a `bytes` value),
    or the
    claimant's opaque 32-byte id once claimed. A claim is permanent — there
    is no way to reset ``claimed_by`` back to ``ZERO32``.

    Attributes:
        amount: Exact capacity this guarantee covers. Must be `> 0`.
        duration: Term in blocks. Must be `> 0` and a power of 2.
        price: Flat fee paid to the guarantor, in ASTR, in full, at claim time.
            Must be `>= 0`.
        expiry: Block height after which an unclaimed guarantee can never be
            claimed. Must be greater than the current block height at post
            time.
        claimed_by: `ZERO32` while available, or the claimant's opaque
            32-byte id once claimed (e.g. a loan transaction hash).
    """

    amount: int
    duration: int
    price: int
    expiry: int
    claimed_by: bytes = ZERO32
    _expr: Optional[Expr] = field(default=None, repr=False, compare=False)

    def to_expr(self) -> Expr:
        """Build (without caching) this guarantee's canonical `Expr` encoding.

        Args:
            None.

        Returns:
            A `link`-list `Expr` of the guarantee's fields in storage order:
            ``[amount, duration, price, expiry, claimed_by]``.
        """
        if self._expr is not None:
            return self._expr
        detail: Expr = link(bytes_(self.claimed_by), NIL)
        detail = link(int_(self.expiry), detail)
        detail = link(int_(self.price), detail)
        detail = link(int_(self.duration), detail)
        detail = link(int_(self.amount), detail)
        return detail

    def expr(self) -> Expr:
        """Return this guarantee's `Expr` encoding, computing and caching it on first use.

        Args:
            None.

        Returns:
            The cached (or freshly built) `Expr` from `to_expr`.
        """
        if self._expr is not None:
            return self._expr
        self._expr = self.to_expr()
        return self._expr

    @classmethod
    def from_storage(
        cls, node: Any, head_hash: bytes, contact=None, block_hash=None
    ) -> TreasuryGuarantee | None:
        """Decode a `TreasuryGuarantee` from its stored `Expr` encoding.

        Args:
            node: Storage node used to resolve any hash-only sub-exprs.
            head_hash: The 32-byte hash of the guarantee's `link`-list head, as
                produced by `to_expr`/`expr`.
            block_hash: The block whose record holds the guarantee's cells.

        Returns:
            The decoded `TreasuryGuarantee`, or `None` if *head_hash* is
            empty/`ZERO32`, unresolvable, or doesn't decode to exactly five
            fields of the expected shape.
        """
        if not head_hash or head_hash == ZERO32:
            return None
        scope = {} if block_hash is None else {"block_hash": block_hash}
        header = get_expr_list(node, head_hash, contact=contact, **scope)
        if header is None or not get_expr_builtin_tag(header) == "link":
            return None
        nodes, missed = resolve_list_exprs(node, header, contact=contact, **scope)
        if missed:
            return None
        if len(nodes) != 5:
            return None
        fields = []
        for n in nodes:
            tag = get_expr_tag(n, node, contact, block_hash)
            if tag == "int":
                fields.append(get_expr_value(n, node, contact, block_hash))
            elif tag == "bytes":
                fields.append(n.value)
            else:
                return None
        if len(fields) != 5:
            return None
        return cls(
            amount=fields[0],
            duration=fields[1],
            price=fields[2],
            expiry=fields[3],
            claimed_by=fields[4],
        )


def encode_borrow_request(request: TreasuryBorrowRequest) -> bytes:
    """Encode a borrow request.

    Args:
        request: The borrow request.

    Returns:
        The encoded bytes.
    """
    if request.payment_interval_blocks <= 0:
        raise ValueError("payment_interval_blocks must be positive")
    if request.payment_count <= 0:
        raise ValueError("payment_count must be positive")

    return (
        bytes([BORROW_REQUEST_VERSION, request.loan_type])
        + request.payment_interval_blocks.to_bytes(U64_SIZE, "little", signed=False)
        + request.payment_count.to_bytes(U64_SIZE, "little", signed=False)
    )


def decode_borrow_request(payload: bytes) -> TreasuryBorrowRequest | None:
    """Decode a borrow request.

    Args:
        payload: The payload bytes.

    Returns:
        The borrow request or None.
    """
    payload_bytes = payload
    if len(payload_bytes) != BORROW_REQUEST_SIZE:
        return None
    if payload_bytes[0] != BORROW_REQUEST_VERSION:
        return None

    try:
        loan_type = LoanType(payload_bytes[1])
    except ValueError:
        return None

    payment_interval_blocks = int.from_bytes(
        payload_bytes[2 : 2 + U64_SIZE],
        "little",
        signed=False,
    )
    payment_count = int.from_bytes(
        payload_bytes[2 + U64_SIZE : BORROW_REQUEST_SIZE],
        "little",
        signed=False,
    )
    if payment_interval_blocks <= 0 or payment_count <= 0:
        return None

    return TreasuryBorrowRequest(
        loan_type=loan_type,
        payment_interval_blocks=payment_interval_blocks,
        payment_count=payment_count,
    )
