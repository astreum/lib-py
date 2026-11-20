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
BORROW_REQUEST_SIZE = 2


# The record of an account in the treasury data trie: balance, loans root,
# total interest paid, loaned, defaulted. A record of no more than these five
# fields is the whole shape; the chain restarts, so no older shape decodes.
USER_RECORD_SHAPE = pair(
    INT,
    pair(
        ROOT_POINTER_SHAPE,
        pair(INT, pair(INT, pair(INT, HOLD))),
    ),
)


class LoanType(IntEnum):
    SECURED = 0
    UNSECURED = 1
    CLAIM_COST = 2


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
            loans of every type, keyed by the transaction hash that created
            each one. `ZERO32` if empty.
        total_interest_paid: Cumulative interest paid across this account's
            loans, including the loan service collected when a claim's
            payout is netted at settlement.
        loaned: Cumulative unsecured principal (`discounted_amount`) this
            account has borrowed, across all its unsecured loans. Feeds the
            insurance rate `p` (see `treasury/borrow.py`).
        defaulted: Cumulative amount permanently written off across this
            account's own unsecured loans, as a borrower. Never reversed.
            Feeds `p`.
    """

    balance: int = 0
    loans_root_hash: bytes = ZERO32
    total_interest_paid: int = 0
    loaned: int = 0
    defaulted: int = 0
    # The block whose record holds the loans trie root's cell, when it is
    # older than the block writing this record: the root is written as a
    # stored ref.
    loans_root_block: Optional[bytes] = field(default=None, compare=False)
    _expr: Optional[Expr] = field(default=None, init=False, repr=False, compare=False)

    def to_expr(self) -> Expr:
        """Build (without caching) this record's canonical `Expr` encoding.

        Args:
            None.

        Returns:
            A `link`-list `Expr` of the record's fields in storage order.
        """
        if self._expr is not None:
            return self._expr
        detail: Expr = link(int_(self.defaulted), NIL)
        detail = link(int_(self.loaned), detail)
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
            contact: The node contact used to resolve hash-only sub-exprs,
                or `None` to use the node's default contact.
            block_hash: The block whose record holds the record's cells. Its
                provider is asked for them, and a trie root held by a plain
                hash is in it. A root written as a stored ref names its own.

        Returns:
            The decoded `TreasuryUserRecord`, or `None` if *head_hash* is
            empty/`ZERO32`, unresolvable, or doesn't decode to exactly five
            fields. The chain restarts, so no older shape is decoded.
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
        blocks: dict[int, Optional[bytes]] = {}
        for i, n in enumerate(nodes):
            if i == 1:  # the loans trie root
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
        if len(fields) != 5:
            return None
        return cls(
            loans_root_block=blocks.get(1),
            balance=fields[0],
            loans_root_hash=fields[1],
            total_interest_paid=fields[2],
            loaned=fields[3],
            defaulted=fields[4],
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
    """A perpetual borrow request.

    The request carries only the loan type. The principal is
    ``transaction.amount``. There is no interval and no count.

    Attributes:
        loan_type: `SECURED` or `UNSECURED`. `CLAIM_COST` exists in
            `LoanType` but is written only by the chain at claim apply, and
            the borrow handler rejects it.
    """

    loan_type: LoanType


@dataclass
class TreasuryLoanRecord:
    """A perpetual treasury loan record.

    The loan has no fixed term or payment count. It carries a per-block
    ``payment_amount`` and tracks how far it is paid through with
    ``paid_until_block``.

    Attributes:
        creation_block_number: Block at which the loan was originated.
        loan_type: `SECURED`, `UNSECURED`, or `CLAIM_COST`.
        discounted_amount: Principal ``L`` the loan is written for. The
            secured handler disburses it; an unsecured principal is minted;
            a claim-cost principal is the financed storage fee. No discount
            applies to the perpetual shape.
        payment_amount: Payment owed per block.
        paid_until_block: Last block through which the loan is paid.
            Equal to ``creation_block_number`` at origination and ``0``
            once the loan is closed.
        insurance_interval_fee: The measured premium this loan pays per block
            of settled service. `0` for `SECURED` loans; priced by the smoothed
            estimator at origination for `UNSECURED` and `CLAIM_COST` loans.
        missed_count: Cumulative count of overdue blocks written off on
            this loan. Always `0` for `SECURED` loans, which have no
            write-off path.
        owner: Current loan owner (32-byte address). The Treasury initially.
            Bought loans are transferred to the buyer.
    """

    creation_block_number: int
    loan_type: LoanType
    discounted_amount: int
    payment_amount: int
    paid_until_block: int
    insurance_interval_fee: int = 0
    missed_count: int = 0
    owner: bytes = TREASURY_ADDRESS
    _expr: Optional[Expr] = field(default=None, init=False, repr=False, compare=False)

    def to_expr(self) -> Expr:
        """Convert loan record to expression.

        Args:
            None.

        Returns:
            The expression representation, with the eight fields in the
            order of the Data Model.
        """
        if self._expr is not None:
            return self._expr
        detail: Expr = link(bytes_(self.owner), NIL)
        detail = link(int_(self.missed_count), detail)
        detail = link(int_(self.insurance_interval_fee), detail)
        detail = link(int_(self.paid_until_block), detail)
        detail = link(int_(self.payment_amount), detail)
        detail = link(int_(self.discounted_amount), detail)
        detail = link(int_(int(self.loan_type)), detail)
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
        """Decode a `TreasuryLoanRecord` from its stored `Expr` encoding.

        Args:
            node: Storage node used to resolve any hash-only sub-exprs.
            head_hash: The 32-byte hash of the record's `link`-list head, as
                produced by `to_expr`/`expr`.
            contact: The node contact used to resolve hash-only sub-exprs,
                or `None` to use the node's default contact.
            block_hash: The block whose record holds the record's cells.

        Returns:
            The decoded `TreasuryLoanRecord`, or `None` if *head_hash* is
            empty/`ZERO32`, unresolvable, or does not decode to exactly
            eight fields. The chain restarts, so no older shape is decoded.
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
        if len(nodes) != 8:
            return None
        int_fields: list[int] = []
        owner = TREASURY_ADDRESS
        for i, n in enumerate(nodes):
            if i == 7:
                if get_expr_builtin_tag(n) != "bytes":
                    return None
                owner = n.value
                continue
            if get_expr_tag(n, node, contact, block_hash) == "int":
                int_fields.append(get_expr_value(n, node, contact, block_hash))
            else:
                return None
        if len(int_fields) != 7:
            return None
        try:
            loan_type = LoanType(int_fields[1])
        except ValueError:
            return None
        return cls(
            creation_block_number=int_fields[0],
            loan_type=loan_type,
            discounted_amount=int_fields[2],
            payment_amount=int_fields[3],
            paid_until_block=int_fields[4],
            insurance_interval_fee=int_fields[5],
            missed_count=int_fields[6],
            owner=owner,
        )


def encode_borrow_request(request: TreasuryBorrowRequest) -> bytes:
    """Encode a perpetual borrow request.

    The payload is ``[version, loan_type]``. The principal is a transaction
    field, so the request carries no interval or count.

    Args:
        request: The borrow request.

    Returns:
        The two-byte encoded request.

    Raises:
        ValueError: If ``request.loan_type`` is not a `LoanType` member.
    """
    try:
        loan_type = LoanType(request.loan_type)
    except ValueError as exc:
        raise ValueError("loan_type must be SECURED, UNSECURED, or CLAIM_COST") from exc
    return bytes([BORROW_REQUEST_VERSION, int(loan_type)])


def decode_borrow_request(payload: bytes) -> TreasuryBorrowRequest | None:
    """Decode a perpetual borrow request.

    Args:
        payload: The payload bytes.

    Returns:
        The borrow request or None if the payload is the wrong size, the
        version differs, or the loan type is unknown.
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

    return TreasuryBorrowRequest(loan_type=loan_type)
