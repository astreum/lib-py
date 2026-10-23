"""The shape of a block header.

A block is an expr tree: a root whose head is the inner pair and whose tail is
the ``block`` symbol. The inner pair is a list of two: the body and the
signature. The body is a list of 18 fields. Ten fields are ints. The decoder
reads each int, which needs the int expr and its payload bytes expr. Without a
shape, each of those is its own request.

This shape asks for all of them at once. It leaves the six link fields
(accounts, bloom, previous block, previous era, receipts, transactions) as
hashes. The decoder never follows them. They are read only when needed.
"""

from __future__ import annotations

from astreum.expression.shape import HOLD, INT, SEND, pair, repeat

# Body fields in the order the decoder reads them.
_BODY_FIELDS = (
    INT,   # global loaned
    INT,   # global defaulted
    INT,   # global loan count
    SEND,  # accounts
    SEND,  # bloom
    INT,   # chain id
    INT,   # difficulty
    INT,   # height
    INT,   # nonce
    SEND,  # previous block
    SEND,  # previous era
    SEND,  # receipts
    INT,   # timestamp
    INT,   # total storage fee
    INT,   # total transaction fee
    SEND,  # transactions
    SEND,  # validator public key
    repeat(repeat(INT)),  # statistics: a list of lists of ints
)


def _body_shape() -> bytes:
    """Build the rule for the 18-field body list.

    Returns:
        bytes: A chain of PAIR rules, one per field.
    """
    rule = HOLD
    for field in reversed(_BODY_FIELDS):
        rule = pair(field, rule)
    return rule


# Root: the inner pair, and the ``block`` symbol. Inner pair: the body, and a
# list of one that holds the signature.
BLOCK_SHAPE = pair(pair(_body_shape(), pair(SEND, HOLD)), SEND)
