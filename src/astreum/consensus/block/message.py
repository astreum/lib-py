"""``BLOCK`` message: a validator announces its newest block's header.

Payload::

    [provider contact: 70 bytes][one STORAGE_FOUND page: block header exprs]

The contact is the announcing validator's provider payload
(``build_provider_payload``): the peer that holds everything else about the
block. It travels inside the payload so a relayed message still points at the
validator, not at the forwarder. The page holds the block expr and its inline
sub-exprs (the header). Tries, transaction bodies and the rest are fetched from
the contact with targeted GETs.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from astreum.communication.message_pow import MAX_INLINE_MESSAGE_BYTES
from astreum.communication.storage_response.storage_found import (
    decode_found_page,
    encode_found_pages,
)
from astreum.communication.storage_response.storage_provider import decode_storage_provider
from astreum.expression import Expr
from astreum.expression.expr import TAG_BYTE_ENCODINGS, bytes_

PROVIDER_PAYLOAD_BYTES = 70

Contact = Tuple[bytes, str, int]


def block_header_exprs(block_expr: Expr) -> List[Expr]:
    """Return *block_expr* and every sub-expr held inline, root first.

    Walks only what is already in memory (``_head`` / ``_tail`` objects): hash
    references such as ``accounts_hash`` are left alone, so this never touches
    storage or the network. The encoded value of each builtin composite (int,
    ...) is included as its own bytes expr.
    """

    Args:
        block_expr (Expr): Description.

    result: List[Expr] = []
    seen: set[bytes] = set()
    stack: List[Expr] = [block_expr]
    while stack:
        expr = stack.pop()
        if expr is None:
            continue
        h = expr.hash()
        if h in seen:
            continue
        seen.add(h)
        result.append(expr)
        if expr.base != "link":
            continue
        # A builtin composite (int, ...) holds its value inline, but its hash
        # refers to the encoded value as a separate bytes expr, which the
        # receiver needs to read the value back.
        if (
            expr._head is None
            and expr.value is not None
            and expr._tail is not None
            and expr._tail.base == "symbol"
        ):
            encoder = TAG_BYTE_ENCODINGS.get(expr._tail.value)
            if encoder is not None:
                stack.append(bytes_(encoder(expr.value)))
        # Push tail first so the head is visited first.
        if expr._tail is not None:
            stack.append(expr._tail)
        if expr._head is not None:
            stack.append(expr._head)
    return result


def encode_block_message(provider_payload: bytes, block_expr: Expr) -> bytes:
    """Encode a ``BLOCK`` payload.

    Raises:
        ValueError: if the provider payload is not 70 bytes or the header does
            not fit one datagram.
    """

    Args:
        provider_payload (bytes): Description.
        block_expr (Expr): Description.

    if len(provider_payload) != PROVIDER_PAYLOAD_BYTES:
        raise ValueError("provider payload must be 70 bytes")

    def _no_skip(expr: Expr, size: int) -> None:

        Args:
            expr (Expr): Description.
            size (int): Description.

        raise ValueError("block header expr too large for one page")

    pages = encode_found_pages(block_header_exprs(block_expr), on_skip=_no_skip)
    if len(pages) != 1:
        raise ValueError("block header does not fit one page")
    payload = provider_payload + pages[0]
    if len(payload) > MAX_INLINE_MESSAGE_BYTES:
        raise ValueError(
            f"block message too large ({len(payload)} > {MAX_INLINE_MESSAGE_BYTES})"
        )
    return payload


def decode_block_message(content: bytes) -> Optional[Tuple[Contact, List[Expr]]]:
    """Decode a ``BLOCK`` payload to ``(contact, exprs)``, root first.

    Pure in-memory. Returns ``None`` on any structural failure. The exprs are
    flat (hash references, as stored), so the first one's ``hash()`` is the
    block hash.
    """

    Args:
        content (bytes): Description.

    if not content or len(content) <= PROVIDER_PAYLOAD_BYTES:
        return None
    try:
        _storage_key, relay_key, address, port = decode_storage_provider(
            content[:PROVIDER_PAYLOAD_BYTES]
        )
        page, total, exprs = decode_found_page(content[PROVIDER_PAYLOAD_BYTES:])
    except Exception:
        return None
    if page != 1 or total != 1 or not exprs:
        return None
    return (relay_key, address, port), exprs
