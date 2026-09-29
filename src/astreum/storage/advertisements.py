import time
from typing import TYPE_CHECKING

from astreum.storage.exprs.network import put_exprs_in_network

if TYPE_CHECKING:
    from astreum import Node


def advertise_exprs(
    node: "Node", entries
) -> tuple[list[bytes], str | None]:
    """Advertise the given expr entries to the network.

    Filters out expired entries, then announces the remaining expr ids,
    bucketed by destination peer and sent as ``STORAGE_PUT`` datagrams
    (``put_exprs_in_network``).

    Args:
        node: A Node instance with ``config``, ``logger`` and the storage/put
            networking infrastructure initialized.
        entries: An iterable of ``(expr_id, payload_type, expires_at)`` tuples,
            where ``expires_at`` is a unix timestamp (``None`` for no expiry).

    Returns:
        A tuple of ``(advertised_ids, warning_reason)`` where ``advertised_ids``
        is the list of expr ids successfully queued for advertisement and
        ``warning_reason`` is a human-readable summary of any failures (or
        ``None`` if all succeeded).
    """
    now = time.time()
    expired = 0
    to_advertise = []
    failed = 0
    first_reason = None
    for entry in entries:
        try:
            expr_id, payload_type, expires_at = entry
        except (TypeError, ValueError):
            node.logger.debug("Invalid expr advertisement entry: %r", entry)
            failed += 1
            if first_reason is None:
                first_reason = "invalid expr advertisement entry"
            continue
        if expires_at is not None:
            try:
                if expires_at <= now:
                    expired += 1
                    continue
            except TypeError:
                node.logger.debug(
                    "Invalid expr advertisement expiry for %s: %r",
                    expr_id.hex(),
                    expires_at,
                )
                failed += 1
                if first_reason is None:
                    first_reason = f"invalid expr advertisement expiry for {expr_id.hex()}"
                continue
        to_advertise.append(entry)

    advertised_ids: list[bytes] = []
    results = put_exprs_in_network(
        node, [(expr_id, payload_type) for expr_id, payload_type, _ in to_advertise]
    )
    for expr_id, queued, reason in results:
        if queued:
            advertised_ids.append(expr_id)
        else:
            failed += 1
            if first_reason is None:
                first_reason = reason

    warning_reason = None
    if failed:
        warning_reason = (
            f"{failed} advertisement(s) failed; first reason: {first_reason or 'unknown'}"
        )

    node.logger.info(
        "Expr advertisement complete (advertised=%s, expired=%s, failed=%s)",
        len(advertised_ids),
        expired,
        failed,
    )
    return advertised_ids, warning_reason
