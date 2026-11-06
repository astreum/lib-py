"""append_sent_transaction: the CSV log of sent transactions."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from astreum.consensus.transaction import Transaction, TransactionCode
from astreum.consensus.transaction import sent_log as sent_log_module
from astreum.consensus.transaction.sent_log import append_sent_transaction

HEADER = "tx_hash,sender,recipient,code,sent_height,sent_time\n"


def _signed() -> Transaction:
    key = Ed25519PrivateKey.generate()
    tx = Transaction(
        chain_id=0,
        amount=5,
        code=TransactionCode.TRANSFER,
        counter=0,
        recipient=os.urandom(32),
        sender=key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )
    tx.sign(key)
    return tx


class TestAppendSentTransaction(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "sent.csv"
        self.node = SimpleNamespace(
            config=SimpleNamespace(sent_transactions_path=str(self.path)),
            logger=mock.Mock(),
        )

    def test_first_append_writes_header_and_line(self) -> None:
        tx = _signed()
        h = os.urandom(32)
        with mock.patch.object(sent_log_module.time, "time", return_value=1234.9):
            append_sent_transaction(self.node, tx, h, 7)
        expected = HEADER + (
            f"{h.hex()},{tx.sender.hex()},{tx.recipient.hex()},{int(tx.code)},7,1234\n"
        )
        self.assertEqual(self.path.read_bytes().decode("utf-8"), expected)

    def test_second_append_adds_one_line(self) -> None:
        tx = _signed()
        append_sent_transaction(self.node, tx, os.urandom(32), 1)
        append_sent_transaction(self.node, tx, os.urandom(32), 2)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines.count(HEADER.strip()), 1)

    def test_empty_file_gets_header(self) -> None:
        self.path.write_bytes(b"")
        append_sent_transaction(self.node, _signed(), os.urandom(32), 1)
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(HEADER))
        self.assertEqual(len(text.splitlines()), 2)

    def test_missing_parent_folder_is_created(self) -> None:
        path = self.dir / "a" / "b" / "sent.csv"
        self.node.config.sent_transactions_path = str(path)
        append_sent_transaction(self.node, _signed(), os.urandom(32), 1)
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_write_error_logs_and_does_not_raise(self) -> None:
        self.path.mkdir()
        append_sent_transaction(self.node, _signed(), os.urandom(32), 1)
        self.node.logger.warning.assert_called_once()

    def test_path_with_nul_byte_logs_and_does_not_raise(self) -> None:
        self.node.config.sent_transactions_path = str(self.dir / "sent\x00.csv")
        append_sent_transaction(self.node, _signed(), os.urandom(32), 1)
        self.node.logger.warning.assert_called_once()

    def test_path_that_is_not_a_string_logs_and_does_not_raise(self) -> None:
        self.node.config.sent_transactions_path = 5.5
        append_sent_transaction(self.node, _signed(), os.urandom(32), 1)
        self.node.logger.warning.assert_called_once()

    def test_bad_transaction_field_logs_and_does_not_raise(self) -> None:
        tx = _signed()
        tx.sender = None
        append_sent_transaction(self.node, tx, os.urandom(32), 1)
        self.node.logger.warning.assert_called_once()
        self.assertFalse(self.path.exists())

    def test_concurrent_appends_write_whole_lines(self) -> None:
        tx = _signed()

        def work() -> None:
            for _ in range(50):
                append_sent_transaction(self.node, tx, os.urandom(32), 1)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 401)
        self.assertEqual(lines.count(HEADER.strip()), 1)
        for line in lines:
            self.assertEqual(len(line.split(",")), 6)
        self.node.logger.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
