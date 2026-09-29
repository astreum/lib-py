from __future__ import annotations

import enum
import time
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from astreum import Node
    from astreum.expression import Expr

DEFAULT_EXPR_REQUEST_TTL_SECONDS = 4.0
DEFAULT_STORAGE_FOUND_MAX_PAGES = 64


class PendingExprRequest:
    """A pending network request plus the reply pages staged for it.

    Attributes:
        payload_type: The requested resolution (SINGLE / LIST / FULL / RECORD).
        expires_at: Idle deadline (``time.monotonic()``); pushed out on every
            accepted page.
        sender: Peer key of the first accepted page, ``None`` until then.
        total: Page count declared by the first accepted page.
        pages: Staged exprs per received page number.
    """

    __slots__ = ("payload_type", "expires_at", "sender", "total", "pages")

    def __init__(self, payload_type: Optional[int], expires_at: float):
        self.payload_type = payload_type
        self.expires_at = expires_at
        self.sender: Optional[bytes] = None
        self.total: Optional[int] = None
        self.pages: Dict[int, List["Expr"]] = {}


class StageResult(enum.Enum):
    IGNORED = "ignored"
    STAGED = "staged"
    COMPLETE = "complete"


def _ttl(node: "Node") -> float:
    """Return the configured pending-request lifetime in seconds."""
    config = getattr(node, "config", None)
    raw = config.get("expr_request_ttl") if isinstance(config, dict) else None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return DEFAULT_EXPR_REQUEST_TTL_SECONDS


def _max_pages(node: "Node") -> int:
    """Return the configured maximum pages per STORAGE_FOUND reply."""
    config = getattr(node, "config", None)
    raw = config.get("storage_found_max_pages") if isinstance(config, dict) else None
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return DEFAULT_STORAGE_FOUND_MAX_PAGES


def _live_entry(node: "Node", expr_id: bytes, now: float):
    """Return the entry for ``expr_id`` or ``None`` if missing/expired.

    Must be called with ``expr_requests_lock`` held. Only looks at the one
    hash; expired entries are left for :func:`prune_expired_expr_reqs`.
    """
    entry = node.expr_requests.get(expr_id)
    if entry is None or entry.expires_at <= now:
        return None
    return entry


def claim_expr_req(node: "Node", expr_id: bytes, resolution: Optional[int] = None) -> bool:
    """Atomically register a pending request unless one is already live.

    Args:
        node: A Node instance with the ``expr_requests`` registry.
        expr_id: The content hash being requested.
        resolution: The resolution strategy attached to the request.

    Returns:
        True if the request was registered (caller should send the GET),
        False if a live request for ``expr_id`` already exists.
    """
    ttl = _ttl(node)
    with node.expr_requests_lock:
        now = time.monotonic()
        if _live_entry(node, expr_id, now) is not None:
            return False
        node.expr_requests[expr_id] = PendingExprRequest(resolution, now + ttl)
        return True


def has_expr_req(node: "Node", expr_id: bytes) -> bool:
    """Return True if a live (unexpired) request is tracked for ``expr_id``."""
    with node.expr_requests_lock:
        return _live_entry(node, expr_id, time.monotonic()) is not None


def stage_found_page(
    node: "Node",
    expr_id: bytes,
    peer_key: bytes,
    page: int,
    total: int,
    exprs: List["Expr"],
) -> Tuple[StageResult, Optional[PendingExprRequest]]:
    """Stage one STORAGE_FOUND page on the pending request for ``expr_id``.

    Bookkeeping only (one lock acquisition, no decoding or storage writes).
    Returns ``(result, request)``; ``request`` is the popped
    :class:`PendingExprRequest` when the result is ``COMPLETE``, else ``None``.

    A page is ignored, changing nothing on the entry, when: the request is
    missing or expired; the first page declares more than
    ``storage_found_max_pages`` pages; the sender is not the one bound by the
    first accepted page; ``total`` differs from the first page's; the page
    number is out of range; or the page was already staged. Every accepted
    page pushes the idle deadline to ``now + expr_request_ttl``. The page that
    completes the set removes the entry, so exactly one caller wins.
    """
    if total < 1 or page < 1 or page > total:
        return StageResult.IGNORED, None
    ttl = _ttl(node)
    max_pages = _max_pages(node)
    with node.expr_requests_lock:
        now = time.monotonic()
        entry = _live_entry(node, expr_id, now)
        if entry is None:
            return StageResult.IGNORED, None
        if entry.sender is None:
            if total > max_pages:
                return StageResult.IGNORED, None
            entry.sender = peer_key
            entry.total = total
        else:
            if peer_key != entry.sender or total != entry.total:
                return StageResult.IGNORED, None
            if page in entry.pages:
                return StageResult.IGNORED, None
        entry.pages[page] = exprs
        entry.expires_at = now + ttl
        if len(entry.pages) == entry.total:
            del node.expr_requests[expr_id]
            return StageResult.COMPLETE, entry
        return StageResult.STAGED, None


def pop_expr_req(node: "Node", expr_id: bytes) -> Optional[int]:
    """Remove the pending request if present and return its payload type.

    Returns:
        The payload type the request was registered with, or ``None`` if no
        live request exists.
    """
    with node.expr_requests_lock:
        entry = node.expr_requests.pop(expr_id, None)
        if entry is None or entry.expires_at <= time.monotonic():
            return None
        return entry.payload_type


def get_expr_req_payload(node: "Node", expr_id: bytes) -> Optional[int]:
    """Return the payload type for a live request without removing it."""
    with node.expr_requests_lock:
        entry = _live_entry(node, expr_id, time.monotonic())
        return None if entry is None else entry.payload_type


def prune_expired_expr_reqs(node: "Node") -> int:
    """Delete every expired request and return how many were removed.

    This is the only full pass over the registry; it runs from the storage
    worker thread, never on the request/response path.
    """
    with node.expr_requests_lock:
        now = time.monotonic()
        expired = [h for h, req in node.expr_requests.items() if req.expires_at <= now]
        for h in expired:
            del node.expr_requests[h]
    return len(expired)
