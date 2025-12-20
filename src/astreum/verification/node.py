from __future__ import annotations

import threading
from queue import Queue
from typing import Any

from astreum.communication.node import connect_node

from .discover import make_discovery_worker
from .worker import make_verify_worker


def verify_blockchain(self: Any):
    """Ensure verification primitives exist, then start discovery and verify workers."""
    connect_node(self)

    self._validation_verify_queue = Queue()

    self.chains = {}
    self.forks = {}
    self.logger.debug(
        "Consensus maps ready for verification (chains=%s, forks=%s)",
        len(self.chains),
        len(self.forks),
    )

    stop_event = getattr(self, "_verify_stop_event", None)
    if stop_event is None:
        stop_event = threading.Event()
        self._verify_stop_event = stop_event
    stop_event.clear()

    discovery_thread = getattr(self, "latest_block_discovery_thread", None)
    if discovery_thread is not None and discovery_thread.is_alive():
        self.logger.debug("Consensus discovery thread already running")
    else:
        discovery_worker = make_discovery_worker(self)
        discovery_thread = threading.Thread(
            target=discovery_worker,
            daemon=True,
            name="latest-block-discovery",
        )
        self.latest_block_discovery_thread = discovery_thread
        discovery_thread.start()
        self.logger.info(
            "Started latest-block discovery thread (%s)", discovery_thread.name
        )

    verify_thread = getattr(self, "verify_thread", None)
    if verify_thread is not None and verify_thread.is_alive():
        self.logger.debug("Consensus verify thread already running")
        return verify_thread

    verify_worker = make_verify_worker(self)
    verify_thread = threading.Thread(
        target=verify_worker, daemon=True, name="verify-worker"
    )
    self.verify_thread = verify_thread
    verify_thread.start()
    self.logger.info("Started verify thread (%s)", verify_thread.name)
    return verify_thread
