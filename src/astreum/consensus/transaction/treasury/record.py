from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

from astreum.expression import Expr, NIL, resolve_list_exprs, link, int_, get_expr_tag, get_expr_value
from astreum.expression import ZERO32
from astreum.storage.exprs import get_expr_list



BORROW_REQUEST_VERSION = 1
BORROW_REQUEST_SIZE = 18
U64_SIZE = 8


class LoanType(IntEnum):
    SECURED = 0
    UNSECURED = 1


@dataclass
class TreasuryUserRecord:
    """Per-account state stored in the treasury account's data trie, keyed
    by the account's own address.

    Attributes:
        balance: Staked ASTR balance backing this account's secured loans.
        loans_root_hash: Root hash of a `RadixTree` of this account's
            secured/unsecured loans, keyed by the `TREASURY_BORROW`
            transaction hash that created each one. `ZERO32` if empty.
        total_interest_paid: Cumulative interest paid across this account's
            repaid/closed loans.
        offers_root_hash: Root hash of a `RadixTree` of this account's
            posted limit offers (`TreasuryCreditOffer`), keyed by the
            `TREASURY_SELL` transaction hash that created each one. `ZERO32`
            if empty.
    """

    balance: int = 0
    loans_root_hash: bytes = ZERO32
    total_interest_paid: int = 0
    offers_root_hash: bytes = ZERO32
    _expr: Optional[Expr] = field(default=None, repr=False, compare=False)

    def to_expr(self) -> Expr:
        """Build (without caching) this record's canonical `Expr` encoding.

        Returns:
            A `link`-list `Expr` of the record's fields in storage order:
            ``[balance, loans_root_hash, total_interest_paid,
            offers_root_hash]``.
        """
        if self._expr is not None:
            return self._expr
        detail: Expr = link(Expr("link", head_hash=self.offers_root_hash), NIL)
        detail = link(int_(self.total_interest_paid), detail)
        detail = link(Expr("link", head_hash=self.loans_root_hash), detail)
        detail = link(int_(self.balance), detail)
        return detail

    def expr(self) -> Expr:
        """Return this record's `Expr` encoding, computing and caching it on
        first use.

        Returns:
            The cached (or freshly built) `Expr` from `to_expr`.
        """
        if self._expr is not None:
            return self._expr
        self._expr = self.to_expr()
        return self._expr

    @classmethod
    def from_storage(cls, node: Any, head_hash: bytes) -> TreasuryUserRecord | None:
        """Decode a `TreasuryUserRecord` from its stored `Expr` encoding.

        Args:
            node: Storage node used to resolve any hash-only sub-exprs.
            head_hash: The 32-byte hash of the record's `link`-list head, as
                produced by `to_expr`/`expr`.

        Returns:
            The decoded `TreasuryUserRecord`, or `None` if *head_hash* is
            empty/`ZERO32`, unresolvable, or doesn't decode to exactly four
            fields of the expected shape.
        """
        if not head_hash or head_hash == ZERO32:
            return None
        header = get_expr_list(node, head_hash)
        if header is None or not header._tag == "link":
            return None
        nodes, missed = resolve_list_exprs(node, header)
        if missed:
            return None
        if len(nodes) != 4:
            return None
        fields = []
        for n in nodes:
            tag = get_expr_tag(n, node)
            if tag == "int":
                fields.append(get_expr_value(n, node))
            elif tag == "link" and (n._head_hash is not None or n.head is NIL):
                fields.append(n._head_hash if n._head_hash is not None else ZERO32)
            else:
                return None
        if len(fields) != 4:
            return None
        return cls(
            balance=fields[0],
            loans_root_hash=fields[1],
            total_interest_paid=fields[2],
            offers_root_hash=fields[3],
        )


@dataclass(frozen=True)
class TreasuryBorrowRequest:
    loan_type: LoanType
    payment_interval_blocks: int
    payment_count: int


@dataclass
class TreasuryLoanRecord:
    creation_block_number: int
    loan_type: LoanType
    discounted_amount: int
    payment_amount: int
    payment_interval_blocks: int
    next_payment_block_number: int
    payment_count: int
    _expr: Optional[Expr] = field(default=None, repr=False, compare=False)

    def to_expr(self) -> Expr:
        if self._expr is not None:
            return self._expr
        detail: Expr = link(int_(self.payment_interval_blocks), NIL)
        detail = link(int_(self.payment_amount), detail)
        detail = link(int_(self.next_payment_block_number), detail)
        detail = link(int_(int(self.loan_type)), detail)
        detail = link(int_(self.payment_count), detail)
        detail = link(int_(self.discounted_amount), detail)
        detail = link(int_(self.creation_block_number), detail)
        return detail

    def expr(self) -> Expr:
        if self._expr is not None:
            return self._expr
        self._expr = self.to_expr()
        return self._expr

    @classmethod
    def from_storage(cls, node: Any, head_hash: bytes) -> TreasuryLoanRecord | None:
        if not head_hash or head_hash == ZERO32:
            return None
        header = get_expr_list(node, head_hash)
        if header is None or not header._tag == "link":
            return None
        nodes, missed = resolve_list_exprs(node, header)
        if missed:
            return None
        if len(nodes) != 7:
            return None
        fields = []
        for n in nodes:
            if get_expr_tag(n, node) == "int":
                fields.append(get_expr_value(n, node))
            else:
                return None
        if len(fields) != 7:
            return None
        try:
            loan_type = LoanType(fields[3])
        except ValueError:
            return None
        return cls(
            creation_block_number=fields[0],
            loan_type=loan_type,
            discounted_amount=fields[1],
            payment_count=fields[2],
            next_payment_block_number=fields[4],
            payment_amount=fields[5],
            payment_interval_blocks=fields[6],
        )


@dataclass
class TreasuryCreditOffer:
    """A limit seller's posted, fixed-size, fixed-duration, fixed-price offer.

    ``claimed_by`` is ``ZERO32`` while available (the same "unset hash"
    convention used by ``TreasuryUserRecord.loans_root_hash``), or the
    claimant's opaque 32-byte id once claimed. A claim is permanent — there
    is no way to reset ``claimed_by`` back to ``ZERO32``.

    Attributes:
        limit: Exact capacity this offer covers. Must be `> 0`.
        duration: Term in blocks. Must be `> 0` and a power of 2.
        price: Flat fee paid to the seller, in ASTR, in full, at claim time.
            Must be `>= 0`.
        expiry: Block height after which an unclaimed offer can never be
            claimed. Must be greater than the current block height at post
            time.
        claimed_by: `ZERO32` while available, or the claimant's opaque
            32-byte id once claimed (e.g. a loan transaction hash).
    """

    limit: int
    duration: int
    price: int
    expiry: int
    claimed_by: bytes = ZERO32
    _expr: Optional[Expr] = field(default=None, repr=False, compare=False)

    def to_expr(self) -> Expr:
        """Build (without caching) this offer's canonical `Expr` encoding.

        Returns:
            A `link`-list `Expr` of the offer's fields in storage order:
            ``[limit, duration, price, expiry, claimed_by]``.
        """
        if self._expr is not None:
            return self._expr
        detail: Expr = link(Expr("link", head_hash=self.claimed_by), NIL)
        detail = link(int_(self.expiry), detail)
        detail = link(int_(self.price), detail)
        detail = link(int_(self.duration), detail)
        detail = link(int_(self.limit), detail)
        return detail

    def expr(self) -> Expr:
        """Return this offer's `Expr` encoding, computing and caching it on
        first use.

        Returns:
            The cached (or freshly built) `Expr` from `to_expr`.
        """
        if self._expr is not None:
            return self._expr
        self._expr = self.to_expr()
        return self._expr

    @classmethod
    def from_storage(cls, node: Any, head_hash: bytes) -> TreasuryCreditOffer | None:
        """Decode a `TreasuryCreditOffer` from its stored `Expr` encoding.

        Args:
            node: Storage node used to resolve any hash-only sub-exprs.
            head_hash: The 32-byte hash of the offer's `link`-list head, as
                produced by `to_expr`/`expr`.

        Returns:
            The decoded `TreasuryCreditOffer`, or `None` if *head_hash* is
            empty/`ZERO32`, unresolvable, or doesn't decode to exactly five
            fields of the expected shape.
        """
        if not head_hash or head_hash == ZERO32:
            return None
        header = get_expr_list(node, head_hash)
        if header is None or not header._tag == "link":
            return None
        nodes, missed = resolve_list_exprs(node, header)
        if missed:
            return None
        if len(nodes) != 5:
            return None
        fields = []
        for n in nodes:
            tag = get_expr_tag(n, node)
            if tag == "int":
                fields.append(get_expr_value(n, node))
            elif tag == "link" and (n._head_hash is not None or n.head is NIL):
                fields.append(n._head_hash if n._head_hash is not None else ZERO32)
            else:
                return None
        if len(fields) != 5:
            return None
        return cls(
            limit=fields[0],
            duration=fields[1],
            price=fields[2],
            expiry=fields[3],
            claimed_by=fields[4],
        )


def encode_borrow_request(request: TreasuryBorrowRequest) -> bytes:
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
