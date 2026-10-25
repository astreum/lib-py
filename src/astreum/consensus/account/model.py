from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from astreum.expression import Expr, NIL, ZERO32, link, int_
from astreum.expression.refs import pointer_item
from astreum.expression.shape import HOLD, INT, SEND, pair
from astreum.storage.radix import RadixTree, clone_radix_tree, get_radix_node_expr


# A trie root field of a record: a stored ref is a list of three cells, a plain
# wrapper is shorter. This rule sends the first three cells with their heads.
ROOT_POINTER_SHAPE = pair(SEND, pair(SEND, pair(SEND, HOLD)))

# The account expr: data root, counter, code root, channels root, balance.
# This is the layout of ``Account.to_expr``.
ACCOUNT_SHAPE = pair(
    ROOT_POINTER_SHAPE,
    pair(
        INT,
        pair(ROOT_POINTER_SHAPE, pair(ROOT_POINTER_SHAPE, pair(INT, HOLD))),
    ),
)


def _root_item(root_hash: bytes, block: Optional[bytes]) -> Expr:
    """Create a link expression for a root hash.

    Args:
        root_hash: The root hash bytes.
        block: Optional block identifier bytes.

    Returns:
        A link expression for the root hash.
    """
    if root_hash == ZERO32:
        return Expr("link", head_hash=ZERO32)
    return pointer_item(root_hash, block)


@dataclass
class Account:
    balance: int
    code_hash: bytes
    counter: int
    data_hash: bytes
    channels_hash: bytes
    data: RadixTree
    channels: RadixTree
    # The block whose record holds the cell each root names, when it is older
    # than the block writing the account: the root is written as a stored ref
    # to that block. None for a root that is new (or ZERO32).
    data_block: Optional[bytes] = None
    code_block: Optional[bytes] = None
    channels_block: Optional[bytes] = None
    # The fields as loaded; an account still equal to them is not written back.
    _loaded: Optional[tuple] = field(default=None, repr=False, compare=False)
    _expr: Optional["Expr"] = field(default=None, repr=False)

    def to_expr(self) -> "Expr":
        """Convert account to expression.

        Args:
            None.

        Returns:
            The expression representation of the account.
        """
        detail: Expr = link(int_(self.balance), NIL)
        detail = link(_root_item(self.channels_hash, self.channels_block), detail)
        detail = link(_root_item(self.code_hash, self.code_block), detail)
        detail = link(int_(self.counter), detail)
        detail = link(_root_item(self.data_hash, self.data_block), detail)
        return detail

    def state_key(self) -> tuple:
        """The fields that make up the account's encoding.

        Args:
            None.

        Returns:
            A tuple of the account's state fields.
        """
        return (
            self.balance, self.counter, self.data_hash, self.code_hash, self.channels_hash,
        )

    def expr(self) -> "Expr":
        """Get the cached expression or compute it.

        Args:
            None.

        Returns:
            The expression representation of the account.
        """
        if self._expr is not None:
            return self._expr
        self._expr = self.to_expr()
        return self._expr

    def clone(self) -> "Account":
        """Create a copy of this account.

        Args:
            None.

        Returns:
            A cloned Account instance.
        """
        cloned = Account(
            balance=self.balance,
            code_hash=self.code_hash,
            counter=self.counter,
            data_hash=self.data_hash,
            channels_hash=self.channels_hash,
            data=clone_radix_tree(self.data),
            channels=clone_radix_tree(self.channels),
            data_block=self.data_block,
            code_block=self.code_block,
            channels_block=self.channels_block,
        )
        cloned._loaded = self._loaded
        if self._expr is not None:
            cloned._expr = self._expr
        return cloned


def extract_account_exprs(account: Account) -> list[Expr]:
    """Collect every expr that must be in storage to reconstruct an Account.

    Args:
        account: The account to extract expressions from.

    Returns:
        A list of all expressions needed to reconstruct the account.
    """
    exprs: list[Expr] = [account.expr()]
    for node in account.data.nodes.values():
        exprs.append(get_radix_node_expr(node))
        val = node.value
        if val is not None and not isinstance(val, bytes):
            exprs.append(val)
    for node in account.channels.nodes.values():
        exprs.append(get_radix_node_expr(node))
        val = node.value
        if val is not None and not isinstance(val, bytes):
            exprs.append(val)
    return exprs
