import queue
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src")

from astreum.communication import outgoing_queue
from astreum.communication.message_pow import MAX_UDP_DATAGRAM_BYTES, NONCE_SIZE


class _Msg:
    topic = None
    sender_public_key_bytes = b"\x00" * 32

    def __init__(self, size: int):
        self._size = size

    def to_bytes(self) -> bytes:
        return b"x" * self._size


def _node():
    return SimpleNamespace(
        config={"relay_public_key_bytes": b"R" * 32},
        logger=MagicMock(),
        outgoing_queue=queue.Queue(),
    )


class TestOutgoingSize(unittest.TestCase):
    def test_exact_limit_is_queued(self):
        node = _node()
        with patch.object(outgoing_queue, "calculate_message_nonce", return_value=1):
            ok = outgoing_queue.enqueue_outgoing(
                node, ("127.0.0.1", 1), _Msg(MAX_UDP_DATAGRAM_BYTES - NONCE_SIZE)
            )
        self.assertTrue(ok)
        payload, _ = node.outgoing_queue.get_nowait()
        self.assertEqual(len(payload), MAX_UDP_DATAGRAM_BYTES)

    def test_one_over_is_rejected_before_pow(self):
        node = _node()
        with patch.object(outgoing_queue, "calculate_message_nonce") as pow_fn:
            ok = outgoing_queue.enqueue_outgoing(
                node, ("127.0.0.1", 1), _Msg(MAX_UDP_DATAGRAM_BYTES - NONCE_SIZE + 1)
            )
        self.assertFalse(ok)
        pow_fn.assert_not_called()
        self.assertTrue(node.outgoing_queue.empty())
        node.logger.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
