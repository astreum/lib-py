from __future__ import annotations

import time
from queue import Empty
from typing import Any, Dict, Set, Tuple


def make_discovery_worker(node: Any):
    """
    Build the discovery worker bound to the given node.

    The returned callable mirrors the previous inline worker in ``setup.py``.
    """

    def _discovery_worker() -> None:
        node.logger.info("Discovery worker started")
        stop = node._verify_stop_event
        while not stop.is_set():
            try:
                peers = getattr(node, "peers", None)
                if isinstance(peers, dict):
                    pairs: list[Tuple[Any, bytes]] = [
                        (peer_id, bytes(latest))
                        for peer_id, peer in list(peers.items())
                        if isinstance(
                            (latest := getattr(peer, "latest_block", None)),
                            (bytes, bytearray),
                        )
                        and latest
                    ]
                    latest_keys: Set[bytes] = {hb for _, hb in pairs}
                    grouped: Dict[bytes, set[Any]] = {
                        hb: {pid for pid, phb in pairs if phb == hb}
                        for hb in latest_keys
                    }

                    if not pairs:
                        node.logger.debug("No peers reported latest blocks; skipping queue update")
                        continue

                    node.logger.debug(
                        "Discovery grouped %d block hashes from %d peers",
                        len(grouped),
                        len(pairs),
                    )

                    try:
                        while True:
                            node._validation_verify_queue.get_nowait()
                    except Empty:
                        pass
                    for latest_b, peer_set in grouped.items():
                        node._validation_verify_queue.put((latest_b, peer_set))
                        node.logger.debug(
                            "Queued %d peers for validation of block %s",
                            len(peer_set),
                            latest_b.hex(),
                        )
            except Exception:
                node.logger.exception("Discovery worker iteration failed")
            finally:
                time.sleep(0.5)

        node.logger.info("Discovery worker stopped")

    return _discovery_worker
