from typing import Callable, List, Optional, Tuple

from astreum.communication.message_pow import MAX_INLINE_MESSAGE_BYTES
from astreum.expression import Expr
from astreum.expression.encoding import encode_expr_to_bytes, decode_expr_from_bytes


STORAGE_FOUND_PAYLOAD = 1

# StorageResponse header: 1B code + 32B expr_id.
_RESPONSE_HEADER_BYTES = 33
# Page header: type 1B + page u16 + total u16 + count u16.
_PAGE_HEADER_BYTES = 7
PAGE_ENTRY_BUDGET = MAX_INLINE_MESSAGE_BYTES - _RESPONSE_HEADER_BYTES - _PAGE_HEADER_BYTES


def _build_page(page: int, total: int, entries: List[bytes]) -> bytes:
    parts = [
        bytes([STORAGE_FOUND_PAYLOAD]),
        page.to_bytes(2, "big", signed=False),
        total.to_bytes(2, "big", signed=False),
        len(entries).to_bytes(2, "big", signed=False),
    ]
    for expr_bytes in entries:
        parts.append(len(expr_bytes).to_bytes(4, "big", signed=False))
        parts.append(expr_bytes)
    return b"".join(parts)


def encode_found_pages(
    exprs: List[Expr],
    on_skip: Optional[Callable[[Expr, int], None]] = None,
) -> List[bytes]:
    """Split ``exprs`` (root first) into ``STORAGE_FOUND`` page payloads.

    Layout of each page::

        [0x01][page u16][total u16][count u16][count x (len u32 + expr bytes)]

    ``page`` is 1-based; a reply that fits one message is page ``1/1``. The
    root goes first in page 1; the rest are packed greedily in order. A
    non-root expr too large for a page is skipped (``on_skip(expr,
    encoded_len)`` is called); an oversized root raises ``ValueError``.
    """
    if not exprs:
        raise ValueError("no exprs to page")

    root_bytes = encode_expr_to_bytes(exprs[0])
    if 4 + len(root_bytes) > PAGE_ENTRY_BUDGET:
        raise ValueError("root expr too large for a STORAGE_FOUND page")

    pages: List[List[bytes]] = [[root_bytes]]
    used = 4 + len(root_bytes)
    for expr in exprs[1:]:
        expr_bytes = encode_expr_to_bytes(expr)
        cost = 4 + len(expr_bytes)
        if cost > PAGE_ENTRY_BUDGET:
            if on_skip is not None:
                on_skip(expr, len(expr_bytes))
            continue
        if used + cost > PAGE_ENTRY_BUDGET:
            pages.append([])
            used = 0
        pages[-1].append(expr_bytes)
        used += cost

    total = len(pages)
    if total > 0xFFFF:
        raise ValueError("too many STORAGE_FOUND pages")
    return [_build_page(i + 1, total, entries) for i, entries in enumerate(pages)]


def decode_found_page(payload: bytes) -> Tuple[int, int, List[Expr]]:
    """Decode a page payload (including the type byte) to ``(page, total, exprs)``."""
    if len(payload) < _PAGE_HEADER_BYTES:
        raise ValueError("truncated page header")
    if payload[0] != STORAGE_FOUND_PAYLOAD:
        raise ValueError("not a STORAGE_FOUND page payload")
    page = int.from_bytes(payload[1:3], "big", signed=False)
    total = int.from_bytes(payload[3:5], "big", signed=False)
    count = int.from_bytes(payload[5:7], "big", signed=False)
    if total == 0:
        raise ValueError("page total must be at least 1")
    if page == 0 or page > total:
        raise ValueError("page number out of range")
    if count == 0:
        raise ValueError("page carries no exprs")

    exprs: List[Expr] = []
    offset = _PAGE_HEADER_BYTES
    for _ in range(count):
        if len(payload) - offset < 4:
            raise ValueError("truncated expr length")
        expr_len = int.from_bytes(payload[offset : offset + 4], "big", signed=False)
        offset += 4
        if expr_len <= 0:
            raise ValueError("invalid expr length")
        end = offset + expr_len
        if end > len(payload):
            raise ValueError("truncated expr payload")
        exprs.append(decode_expr_from_bytes(payload[offset:end]))
        offset = end
    if offset != len(payload):
        raise ValueError("trailing bytes after page entries")
    return page, total, exprs


def found_page_expr_bytes(payload: bytes) -> int:
    """Total expr bytes carried by a page payload (excludes framing)."""
    count = int.from_bytes(payload[5:7], "big", signed=False)
    return len(payload) - _PAGE_HEADER_BYTES - 4 * count
