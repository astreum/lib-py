from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from astreum.expression import Expr, NIL, link, int_, bytes_, symbol, ZERO32
from astreum.consensus.transaction.code import TransactionCode


@dataclass
class Transaction:
    chain_id: int
    amount: int
    code: TransactionCode
    counter: int
    cost_limit: int = 0
    data: Expr = NIL
    recipient: bytes = b""
    sender: bytes = b""
    signature: Optional[bytes] = None
    expr_id: Optional[bytes] = None
    body_hash: Optional[bytes] = None
    hash: Optional[bytes] = None
    block_hash: Optional[bytes] = None
    pending_bloom_keys: set[bytes] = field(default_factory=set, repr=False)
    pending_bloom_inserts: set[bytes] = field(default_factory=set, repr=False)
    _expr: Optional["Expr"] = field(default=None, repr=False)

    def _body(self) -> Expr:
        """Build the signed body: the list of the eight transaction fields."""
        body: Expr = link(bytes_(self.sender), NIL)
        body = link(bytes_(self.recipient), body)
        body = link(self.data, body)
        body = link(int_(self.counter), body)
        body = link(int_(self.cost_limit), body)
        body = link(int_(self.code), body)
        body = link(int_(self.chain_id), body)
        body = link(int_(self.amount), body)
        return body

    def sign(self, private_key: Any) -> bytes:
        """Sign the transaction detail list head and store the signature."""
        body_hash = self._body().hash()
        self.signature = private_key.sign(body_hash)
        self.body_hash = body_hash
        self.expr_id = None
        self.hash = None
        return body_hash

    def verify_signature(self) -> bool:
        """Check that the sender signed the transaction fields.

        The body hash is built again from the fields. The stored
        ``body_hash`` is not trusted.

        Returns:
            True if ``signature`` is a valid Ed25519 signature of the body
            hash by the key in ``sender``. False for a missing signature, a
            wrong length, or a signature that does not match.
        """
        signature = self.signature
        if not isinstance(signature, (bytes, bytearray)) or len(signature) != 64:
            return False
        if not isinstance(self.sender, (bytes, bytearray)) or len(self.sender) != 32:
            return False
        try:
            public_key = Ed25519PublicKey.from_public_bytes(bytes(self.sender))
            public_key.verify(bytes(signature), self._body().hash())
        except Exception:
            return False
        return True

    def to_expr(self) -> Expr:
        body = self._body()
        return link(
            link(body, link(bytes_(self.signature or ZERO32), NIL)),
            symbol("transaction"))

    def expr(self) -> Expr:
        if self._expr is not None:
            return self._expr
        self._expr = self.to_expr()
        return self._expr
