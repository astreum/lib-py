import errno
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.utils import logging as logging_utils
from astreum.utils.config import NodeConfig


class TestLoggingResilience(unittest.TestCase):
    def test_file_handler_disables_after_no_space_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            handler = logging_utils.CSVTimedRotatingFileHandler(
                filename=str(Path(tmp_dir) / "node.csv"),
                when="midnight",
                interval=1,
                backupCount=1,
                utc=True,
                encoding="utf-8",
                delay=True,
            )
            handler.setFormatter(logging_utils.CSVFormatter())

            stream = MagicMock()
            stream.flush.side_effect = OSError(errno.ENOSPC, "No space left on device")
            handler.stream = stream

            record = logging.makeLogRecord({"msg": "hello", "levelno": logging.INFO, "levelname": "INFO"})

            handler.emit(record)
            handler.emit(record)

            self.assertTrue(handler._disabled)
            self.assertEqual(stream.write.call_count, 1)
            stream.close.assert_called_once()

    def test_logging_setup_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir) / "logs"
            with patch.object(logging_utils, "_derive_instance_id", return_value="test-instance"):
                with patch.object(logging_utils, "_log_root", return_value=log_dir):
                    logger = logging_utils.logging_setup(
                        NodeConfig(logging_enabled=False, logger_name="node a")
                    )

            self.assertFalse(log_dir.exists())
            self.assertFalse(hasattr(logger, "_queue_listener"))
            self.assertEqual(logger.extra["logger_name"], "node a")
            self.assertEqual(len(logger.logger.handlers), 1)
            self.assertIsInstance(logger.logger.handlers[0], logging.NullHandler)

            logger.info("this should be ignored")


if __name__ == "__main__":
    unittest.main()
