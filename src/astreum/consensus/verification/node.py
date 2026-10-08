from __future__ import annotations

import threading
from typing import Any

from astreum.communication.node import connect_node
from astreum.consensus.fork.node import fork_setup
from astreum.consensus.verification.worker import make_verify_worker


def verify_blockchain(node: Any):
    """Ensure verification primitives exist, then start verify worker.

    Args:
        node: The node to verify.

    Returns: None
    """
    connect_node(node)

    fork_setup(node)

    stop_event = getattr(node, "_verify_stop_event", None)
    if stop_event is None:
        stop_event = threading.Event()
        node._verify_stop_event = stop_event
    stop_event.clear()

    verify_thread = getattr(node, "verify_thread", None)
    if verify_thread is not None and verify_thread.is_alive():
        return verify_thread

    verify_worker = make_verify_worker(node)
    verify_thread = threading.Thread(
        target=verify_worker, daemon=True, name="verify-worker"
    )
    node.verify_thread = verify_thread
    verify_thread.start()
    node.logger.info("Started verify thread (%s)", verify_thread.name)
    return verify_thread
