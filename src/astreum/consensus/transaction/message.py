from __future__ import annotations

from typing import List, Optional, TYPE_CHECKING

from astreum.expression import Expr, NIL, ZERO32
from astreum.expression.expr import (
    TAG_BYTE_ENCODINGS,
    TAG_BYTE_DECODINGS,
    TYPE_SYMBOLS,
    BUILTIN_COMPOSITE_TYPE_NAMES,
    bytes_,
)
from astreum.communication.storage_response.storage_found import (
    decode_found_page,
    encode_found_pages,
)

if TYPE_CHECKING:
    from astreum.consensus.transaction.model import Transaction


# Builtin composite type symbols are hardcoded singletons known to every node
# (TYPE_SYMBOLS, with precomputed hashes), so they never need to travel on the
# wire: the receiver rebuilds them from their hashes.
_KNOWN_TYPE_EXPRS = {s.hash(): s for s in TYPE_SYMBOLS.values()}


def _encode_single_page(exprs: List[Expr]) -> bytes:
    """Frame ``exprs`` as one ``STORAGE_FOUND`` page (``1/1``)."""
    def _no_skip(expr: Expr, size: int) -> None:
        raise ValueError("transaction expr too large for one page")

    pages = encode_found_pages(exprs, on_skip=_no_skip)
    if len(pages) != 1:
        raise ValueError("transaction message does not fit one page")
    return pages[0]


def encode_transaction_message(tx_exprs: List[Expr]) -> bytes:
    """Encode the whole resolved tx tree as a TRANSACTION message payload.

    ``tx_exprs`` is the output of ``resolve_inner_exprs(node, transaction.expr())``
    with the header root first. Uses the storage_found length-prefixed list
    framing with the standard expr byte encoding. Every expr is emitted once
    (dedup by hash) and builtin composite type symbols are skipped; builtin
    composite values are materialized as plain bytes exprs so the tree
    round-trips in-memory.
    """
    expanded: List[Expr] = []
    seen = set()

    def _add(e: Expr) -> None:
        if e is None:
            return
        h = e.hash()
        if h in seen:
            return
        seen.add(h)
        expanded.append(e)

    def _walk(e: Expr) -> None:
        if e is None:
            return
        if e.hash() in _KNOWN_TYPE_EXPRS:
            return
        _add(e)
        if e.base != "link":
            return
        if (
            e.value is not None
            and e.tail is not None
            and e.tail.base == "symbol"
            and e.tail.value in BUILTIN_COMPOSITE_TYPE_NAMES
        ):
            _add(bytes_(TAG_BYTE_ENCODINGS[e.tail.value](e.value)))
            return
        _walk(e.head)
        _walk(e.tail)

    _walk(tx_exprs[0])
    return _encode_single_page(expanded)


def decode_transaction_message(content: bytes) -> Optional["Transaction"]:
    """Decode a TRANSACTION message payload into a fully-linked Transaction.

    Pure in-memory — zero storage reads/writes. Returns None on any structural
    or parse failure.
    """
    if not content:
        return None
    try:
        page, total, exprs = decode_found_page(content)
    except Exception:
        return None
    if page != 1 or total != 1 or not exprs:
        return None

    expr_map = {e.hash(): e for e in exprs}
    expr_map.update(_KNOWN_TYPE_EXPRS)

    root = exprs[0]
    if root.base != "link":
        return None

    try:
        _link_tree(root, expr_map)
    except KeyError:
        return None

    _reconstruct_builtin_composites(root)

    if root.tail is None or not (root.tail.base == "symbol" and root.tail.value == "transaction"):
        return None

    try:
        return _parse_transaction(root)
    except Exception:
        return None


def _reconstruct_builtin_composites(root: Expr) -> None:
    """Turn decoded ``head=bytes + tail=type-symbol`` links back into builtin
    composites with the raw ``value`` inline (the canonical ``int_()`` form)."""
    visited = set()

    def _rec(e: Expr) -> None:
        if e is None or e.base != "link":
            return
        if id(e) in visited:
            return
        visited.add(id(e))

        if (
            e.head is not None
            and e.head.base == "bytes"
            and e.tail is not None
            and e.tail.base == "symbol"
            and e.tail.value in BUILTIN_COMPOSITE_TYPE_NAMES
        ):
            e.value = TAG_BYTE_DECODINGS[e.tail.value](e.head.value)
            e.head = None
            e.head_hash = None
            return

        _rec(e.head)
        _rec(e.tail)

    _rec(root)


def _link_tree(root: Expr, expr_map: dict[bytes, Expr]) -> None:
    """Back-fill every ``head_hash``/``tail_hash`` from the decoded set so the
    tree is fully linked in-memory and resolution never touches storage."""
    visited = set()

    def _link(e: Expr) -> None:
        if e is None or e.base != "link":
            return
        if id(e) in visited:
            return
        visited.add(id(e))

        if e.head is None:
            if e.head_hash is not None:
                if e.head_hash == ZERO32:
                    e.head = NIL
                else:
                    e.head = expr_map[e.head_hash]
                e.head_hash = None
        if e.tail is None:
            if e.tail_hash is not None:
                if e.tail_hash == ZERO32:
                    e.tail = None
                else:
                    e.tail = expr_map[e.tail_hash]
                e.tail_hash = None

        if e.head is not None:
            _link(e.head)
        if e.tail is not None:
            _link(e.tail)

    _link(root)


def _list_items(head: Expr) -> List[Expr]:
    """Storage-free analogue of resolve_list_exprs: return the head of each
    link in the chain (works only on a fully-linked tree)."""
    result: List[Expr] = []
    current = head
    while current is not None and current.base == "link":
        if current.head is not None:
            result.append(current.head)
        current = current.tail
    return result


def _parse_transaction(root: Expr) -> "Transaction":
    """Storage-free analogue of get_transaction_from_storage. The tree is fully
    linked, so only structural/type validation happens here."""
    from astreum.consensus.transaction.code import TransactionCode
    from astreum.consensus.transaction.model import Transaction

    inner = root.head
    if inner is None or inner.base != "link":
        raise ValueError("transaction inner header must be a Link")

    inner_nodes = _list_items(inner)
    if len(inner_nodes) != 2:
        raise ValueError("malformed transaction header length")

    body, sig = inner_nodes
    if sig.base != "bytes":
        raise ValueError("invalid transaction signature: expected Bytes")
    signature_bytes = sig.value
    if body.base != "link":
        raise ValueError("transaction body must be a Link chain")

    body_nodes = _list_items(body)
    if len(body_nodes) != 8:
        raise ValueError("malformed transaction body length")

    (
        amount_node,
        chain_id_node,
        code_node,
        cost_limit_node,
        counter_node,
        data_node,
        recipient_node,
        sender_node,
    ) = body_nodes

    if amount_node._tag != "int":
        raise ValueError("expected Int for amount")
    if chain_id_node._tag != "int":
        raise ValueError("expected Int for chain_id")
    if code_node._tag != "int":
        raise ValueError("expected Int for code")
    if cost_limit_node._tag != "int":
        raise ValueError("expected Int for cost_limit")
    if counter_node._tag != "int":
        raise ValueError("expected Int for counter")
    if recipient_node._tag != "bytes":
        raise ValueError("expected Bytes for recipient")
    if sender_node._tag != "bytes":
        raise ValueError("expected Bytes for sender")

    tx_hash = root.hash()
    tx = Transaction(
        chain_id=chain_id_node.value,
        amount=amount_node.value,
        code=TransactionCode(code_node.value),
        counter=counter_node.value,
        cost_limit=cost_limit_node.value,
        data=data_node,
        recipient=recipient_node.value,
        sender=sender_node.value,
        signature=signature_bytes,
        body_hash=body.hash(),
        expr_id=tx_hash,
        hash=tx_hash,
    )
    tx._expr = root
    return tx
