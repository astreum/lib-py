import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.node import Node
from astreum.utils import config as config_module
from astreum.utils.config import config_setup

# (key, minimum, default). minimum: None, 0 (non-negative) or 1 (positive).
INT_KEYS = [
    ("hot_storage_limit", None, config_module.DEFAULT_HOT_STORAGE_LIMIT),
    ("cold_storage_limit", None, config_module.DEFAULT_COLD_STORAGE_LIMIT),
    ("logging_retention_days", None, config_module.DEFAULT_LOGGING_RETENTION_DAYS),
    ("incoming_queue_size_limit", 0, config_module.DEFAULT_INCOMING_QUEUE_SIZE_LIMIT_BYTES),
    ("peer_timeout", 1, config_module.DEFAULT_PEER_TIMEOUT_SECONDS),
    ("peer_timeout_interval", 1, config_module.DEFAULT_PEER_TIMEOUT_INTERVAL_SECONDS),
    ("bootstrap_retry_interval", 1, config_module.DEFAULT_BOOTSTRAP_RETRY_INTERVAL_SECONDS),
    ("self_lookup_rounds", 0, config_module.DEFAULT_SELF_LOOKUP_ROUNDS),
    ("self_lookup_interval", 0, config_module.DEFAULT_SELF_LOOKUP_INTERVAL_SECONDS),
    ("storage_index_interval", 1, config_module.DEFAULT_STORAGE_INDEX_INTERVAL_SECONDS),
    ("storage_request_minimum_price", 0, config_module.DEFAULT_STORAGE_REQUEST_MINIMUM_PRICE),
    ("storage_fetch_retries", 0, config_module.DEFAULT_STORAGE_FETCH_RETRIES),
    ("fair_use_limit", 0, config_module.DEFAULT_FAIR_USE_LIMIT_BYTES),
    ("verification_max_stale_seconds", 0, config_module.DEFAULT_VERIFICATION_MAX_STALE_SECONDS),
    ("verification_max_future_skew", 0, config_module.DEFAULT_VERIFICATION_MAX_FUTURE_SKEW_SECONDS),
    ("message_timestamp_window", 0, config_module.DEFAULT_MESSAGE_TIMESTAMP_WINDOW_SECONDS),
]

# (key, allow_zero, default)
FLOAT_KEYS = [
    ("incoming_queue_timeout", True, config_module.DEFAULT_INCOMING_QUEUE_TIMEOUT_SECONDS),
    ("verify_blockchain_interval", False, float(config_module.DEFAULT_PEER_TIMEOUT_INTERVAL_SECONDS)),
    ("storage_request_price_interval", False, config_module.DEFAULT_STORAGE_REQUEST_PRICE_INTERVAL_SECONDS),
    ("storage_fetch_interval", True, config_module.DEFAULT_STORAGE_FETCH_INTERVAL_SECONDS),
    ("long_term_storage_interval", False, config_module.DEFAULT_LONG_TERM_STORAGE_INTERVAL_SECONDS),
    ("fair_use_ratio", True, config_module.DEFAULT_FAIR_USE_RATIO),
    ("storage_request_flush_interval", False, config_module.DEFAULT_STORAGE_REQUEST_FLUSH_INTERVAL_SECONDS),
]

RANGE_WORDS = {0: "a non-negative integer", 1: "a positive integer"}


class _ConfigCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        accounts = Path(self._tmp.name) / "accounts"
        accounts.mkdir()
        patcher = mock.patch(
            "astreum.utils.data.ensure_data_dir", return_value=Path(self._tmp.name)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, **overrides):
        return config_setup({"default_seed": None, **overrides})


class TestIntegerKeys(_ConfigCase):
    def test_integer_keys_table(self):
        for key, minimum, default in INT_KEYS:
            with self.subTest(key=key):
                self.assertEqual(self.build()[key], default)
                with self.assertRaises(ValueError) as ctx:
                    self.build(**{key: "soon"})
                self.assertEqual(str(ctx.exception), f"{key} must be an integer: 'soon'")
                if minimum is not None:
                    with self.assertRaises(ValueError) as ctx:
                        self.build(**{key: -1 if minimum == 0 else 0})
                    self.assertEqual(
                        str(ctx.exception), f"{key} must be {RANGE_WORDS[minimum]}"
                    )
                result = self.build(**{key: "7"})
                self.assertEqual(result[key], 7)
                self.assertIsInstance(result[key], int)

    def test_port(self):
        self.assertEqual(self.build()["port"], config_module.DEFAULT_PORT)
        self.assertEqual(self.build(port=0)["port"], 0)
        with self.assertRaises(ValueError) as ctx:
            self.build(port=-1)
        self.assertEqual(str(ctx.exception), "port must be 0 or a positive integer")
        with self.assertRaises(ValueError) as ctx:
            self.build(port="x")
        self.assertEqual(str(ctx.exception), "port must be an integer: 'x'")


class TestFloatKeys(_ConfigCase):
    def test_float_keys_table(self):
        for key, allow_zero, default in FLOAT_KEYS:
            with self.subTest(key=key):
                self.assertEqual(self.build()[key], default)
                with self.assertRaises(ValueError) as ctx:
                    self.build(**{key: "soon"})
                self.assertEqual(str(ctx.exception), f"{key} must be a number: 'soon'")
                word = "a non-negative number" if allow_zero else "a positive number"
                bad = -1 if allow_zero else 0
                with self.assertRaises(ValueError) as ctx:
                    self.build(**{key: bad})
                self.assertEqual(str(ctx.exception), f"{key} must be {word}")
                result = self.build(**{key: "2.5"})
                self.assertEqual(result[key], 2.5)
                self.assertIsInstance(result[key], float)


class TestBoolKeys(_ConfigCase):
    def test_bool_keys(self):
        for key, default in (("is_storage_provider", False), ("logging_enabled", True)):
            with self.subTest(key=key):
                self.assertEqual(self.build()[key], default)
                for bad in ("yes", 1):
                    with self.assertRaises(ValueError) as ctx:
                        self.build(**{key: bad})
                    self.assertEqual(str(ctx.exception), f"{key} must be a boolean")
                self.assertTrue(self.build(**{key: True})[key])
                self.assertFalse(self.build(**{key: False})[key])

    def test_bool_accepted_as_int_where_it_is_today(self):
        self.assertEqual(self.build(port=True)["port"], 1)
        self.assertEqual(self.build(hot_storage_limit=True)["hot_storage_limit"], 1)

    def test_bool_rejected_where_it_is_today(self):
        for key in ("storage_put_batch_max_bytes", "storage_get_batch_max_bytes"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    self.build(**{key: True})
                self.assertEqual(str(ctx.exception), f"{key} must be an integer: True")
        with self.assertRaises(ValueError) as ctx:
            self.build(storage_request_flush_interval=True)
        self.assertEqual(
            str(ctx.exception), "storage_request_flush_interval must be a number: True"
        )
        for key in ("expr_request_ttl",):
            with self.assertRaises(ValueError):
                self.build(**{key: True})
        with self.assertRaises(ValueError):
            self.build(storage_found_max_pages=True)


class TestAliasesAndDefaults(_ConfigCase):
    def test_aliases(self):
        self.assertEqual(self.build(hot_storage_default_limit="7")["hot_storage_limit"], 7)
        self.assertEqual(self.build(logging_retention="4")["logging_retention_days"], 4)
        self.assertEqual(self.build(retention_days="2")["logging_retention_days"], 2)

    def test_primary_key_wins_over_alias(self):
        result = self.build(hot_storage_limit=5, hot_storage_default_limit=7)
        self.assertEqual(result["hot_storage_limit"], 5)
        result = self.build(
            logging_retention_days=1, logging_retention=2, retention_days=3
        )
        self.assertEqual(result["logging_retention_days"], 1)

    def test_verify_interval_default_follows_peer_interval(self):
        result = self.build(peer_timeout_interval=30)
        self.assertEqual(result["verify_blockchain_interval"], 30.0)

    def test_expr_request_ttl_default_follows_fetch_settings(self):
        result = self.build(storage_fetch_retries=5, storage_fetch_interval=1)
        self.assertEqual(result["expr_request_ttl"], 10.0)
        result = self.build(storage_fetch_retries=0)
        self.assertEqual(
            result["expr_request_ttl"], config_module.DEFAULT_EXPR_REQUEST_TTL_SECONDS
        )

    def test_unknown_keys_are_ignored(self):
        self.assertFalse(self.build(long_term_storage=True)["is_storage_provider"])


class TestOrderAndMutation(_ConfigCase):
    def test_error_order(self):
        with self.assertRaises(ValueError) as ctx:
            self.build(peer_timeout="x", port="x")
        self.assertEqual(str(ctx.exception), "port must be an integer: 'x'")
        with self.assertRaises(ValueError) as ctx:
            self.build(storage_fetch_retries="x", peer_timeout="x")
        self.assertEqual(str(ctx.exception), "peer_timeout must be an integer: 'x'")
        with self.assertRaises(ValueError) as ctx:
            self.build(fair_use_ratio="x", storage_put_batch_max_bytes="x")
        self.assertEqual(
            str(ctx.exception), "storage_put_batch_max_bytes must be an integer: 'x'"
        )

    def test_no_shared_default(self):
        for func in (config_setup, Node.__init__):
            default = inspect.signature(func).parameters["config"].default
            self.assertIsNone(default, func)
        first = config_setup()
        second = config_setup()
        self.assertIsNot(first, second)
        self.assertEqual(
            inspect.signature(config_setup).parameters["config"].default, None
        )

    def test_none_gives_fresh_dict_with_defaults(self):
        result = config_setup(None)
        self.assertEqual(result["port"], config_module.DEFAULT_PORT)
        self.assertIn("storage_secret_key", result)

    def test_explicit_dict_changes_in_place(self):
        given = {"default_seed": None}
        returned = config_setup(given)
        self.assertIs(returned, given)
        self.assertIn("port", given)


class TestParsers(unittest.TestCase):
    def test_parse_int(self):
        parse = config_module._parse_int
        self.assertEqual(parse("k", "5"), 5)
        self.assertEqual(parse("k", -3), -3)
        self.assertEqual(parse("k", 0, 0), 0)
        with self.assertRaises(ValueError):
            parse("k", None)
        with self.assertRaises(ValueError):
            parse("k", -1, 0)
        with self.assertRaises(ValueError):
            parse("k", 0, 1)

    def test_parse_float(self):
        parse = config_module._parse_float
        self.assertEqual(parse("k", "1.5", False), 1.5)
        self.assertEqual(parse("k", 0, True), 0.0)
        with self.assertRaises(ValueError):
            parse("k", 0, False)
        with self.assertRaises(ValueError):
            parse("k", -0.1, True)
        with self.assertRaises(ValueError):
            parse("k", None, True)

    def test_parse_bool(self):
        parse = config_module._parse_bool
        self.assertIs(parse("k", True), True)
        with self.assertRaises(ValueError):
            parse("k", 1)


if __name__ == "__main__":
    unittest.main()
