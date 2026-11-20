from __future__ import annotations

import logging
import socket
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.communication.processors.incoming import (  # noqa: E402
    populate_incoming_messages,
)


class TestIncomingPopulator(unittest.TestCase):
    def test_closed_socket_stops_the_loop(self) -> None:
        """A closed socket cannot recover; the loop must exit, not spin."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.close()
        node = SimpleNamespace(
            communication_stop_event=threading.Event(),
            socket=sock,
            logger=logging.getLogger("test_incoming_populator"),
        )
        thread = threading.Thread(
            target=populate_incoming_messages,
            args=(node,),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(
            thread.is_alive(),
            "populate_incoming_messages must stop when the socket is closed",
        )


if __name__ == "__main__":
    unittest.main()
