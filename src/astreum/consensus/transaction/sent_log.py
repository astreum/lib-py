from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astreum.consensus.transaction.model import Transaction

SENT_LOG_HEADER = "tx_hash,sender,recipient,code,sent_height,sent_time\n"

_append_lock = threading.Lock()


def append_sent_transaction(
    node: "Node",
    transaction: "Transaction",
    tx_hash: bytes,
    sent_height: int,
) -> None:
    """Append one line for a sent transaction to the sent transaction log.

    The log is a UTF-8 CSV file at ``node.config.sent_transactions_path``. The
    header line is written when the file does not exist or is empty. Each line
    is written with one write call, so a reader that stops at the last ``\\n``
    reads only complete lines. The folder of the file is created when it is
    missing.

    This function never raises. On any error, while it builds or writes the
    line, it logs one warning and returns.

    Args:
        node: The node. Its ``config`` holds the log path.
        transaction: The transaction that was sent.
        tx_hash: The hash of the transaction.
        sent_height: The height of the latest block when the send started.

    Returns: None
    """
    try:
        path = Path(node.config.sent_transactions_path)
        line = (
            f"{tx_hash.hex()},{transaction.sender.hex()},{transaction.recipient.hex()},"
            f"{int(transaction.code)},{int(sent_height)},{int(time.time())}\n"
        )
        with _append_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="") as handle:
                if handle.tell() == 0:
                    line = SENT_LOG_HEADER + line
                handle.write(line)
    except Exception as exc:
        node.logger.warning(
            "Sent transaction log write failed for tx %r at %r: %r",
            tx_hash,
            getattr(node.config, "sent_transactions_path", None),
            exc,
        )
