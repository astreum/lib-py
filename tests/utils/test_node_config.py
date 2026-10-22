import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum import NodeConfig as ExportedNodeConfig
from astreum.utils import config as config_module
from astreum.utils.config import NodeConfig, config_setup

# Fields that hold keys or the default seed. They differ between NodeConfig()
# and config_setup() on purpose: setup generates or derives them.
KEY_FIELDS = {
    "storage_secret_key",
    "storage_public_key",
    "storage_public_key_bytes",
    "validation_public_key",
    "validation_public_key_bytes",
}

# Fields that config_setup does not set. The caller or a later setup step does.
LATER_FIELDS = {
    "use_ipv6",
    "verbose",
    "logger_name",
    "relay_secret_key",
    "relay_public_key",
    "relay_public_key_bytes",
    "latest_block_hash",
}


class TestNodeConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        (Path(self._tmp.name) / "accounts").mkdir()
        patcher = mock.patch(
            "astreum.utils.data.ensure_data_dir",
            return_value=Path(self._tmp.name),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_export(self):
        self.assertIs(ExportedNodeConfig, NodeConfig)

    def test_defaults_match_config_setup(self):
        pure = vars(NodeConfig())
        built = vars(config_setup({"default_seed": config_module.DEFAULT_SEED}))
        for name, value in pure.items():
            if name in KEY_FIELDS:
                continue
            with self.subTest(field=name):
                self.assertEqual(built[name], value)

    def test_every_field_is_built(self):
        built = vars(config_setup({"default_seed": None}))
        self.assertEqual(set(built), set(vars(NodeConfig())))

    def test_values_reach_the_fields(self):
        result = config_setup(
            {
                "default_seed": None,
                "port": "9",
                "use_ipv6": True,
                "verbose": True,
                "logger_name": "demo",
                "latest_block_hash": "ab" * 32,
                "relay_secret_key": "cd" * 32,
            }
        )
        self.assertEqual(result.port, 9)
        self.assertTrue(result.use_ipv6)
        self.assertTrue(result.verbose)
        self.assertEqual(result.logger_name, "demo")
        self.assertEqual(result.latest_block_hash, "ab" * 32)
        self.assertEqual(result.relay_secret_key, "cd" * 32)

    def test_later_fields_default_when_absent(self):
        result = config_setup({"default_seed": None})
        self.assertFalse(result.use_ipv6)
        self.assertFalse(result.verbose)
        for name in LATER_FIELDS - {"use_ipv6", "verbose"}:
            with self.subTest(field=name):
                self.assertIsNone(getattr(result, name))

    def test_unknown_keys_ignored(self):
        result = config_setup({"default_seed": None, "long_term_storage": True})
        self.assertFalse(hasattr(result, "long_term_storage"))
        self.assertFalse(result.is_storage_provider)

    def test_input_dict_unchanged(self):
        given = {"default_seed": None}
        config_setup(given)
        self.assertEqual(given, {"default_seed": None})

    def test_default_pure(self):
        with mock.patch(
            "astreum.utils.data.ensure_data_dir", side_effect=AssertionError
        ), mock.patch.object(Path, "mkdir", side_effect=AssertionError):
            config = NodeConfig()
        self.assertEqual(config.port, config_module.DEFAULT_PORT)

    def test_keyword_arguments(self):
        config = NodeConfig(port=1, message_timestamp_window=0)
        self.assertEqual(config.port, 1)
        self.assertEqual(config.message_timestamp_window, 0)
        self.assertEqual(config.peer_timeout, config_module.DEFAULT_PEER_TIMEOUT_SECONDS)

    def test_additional_seeds_not_shared(self):
        first, second = NodeConfig(), NodeConfig()
        first.additional_seeds.append("a:1")
        self.assertEqual(second.additional_seeds, [])
        given = ["x:1"]
        self.assertIsNot(NodeConfig(additional_seeds=given).additional_seeds, given)

    def test_no_dict_access(self):
        config = NodeConfig()
        with self.assertRaises(TypeError):
            config["port"]
        with self.assertRaises(AttributeError):
            config.get("port")
        with self.assertRaises(AttributeError):
            config.prot

    def test_runtime_writes(self):
        config = NodeConfig()
        for name, value in (
            ("port", 5),
            ("relay_public_key", object()),
            ("relay_public_key_bytes", b"\x01" * 32),
            ("validation_secret_key", object()),
            ("validation_public_key", object()),
            ("validation_public_key_bytes", b"\x02" * 32),
            ("cold_storage_path", None),
        ):
            with self.subTest(field=name):
                setattr(config, name, value)
                self.assertIs(getattr(config, name), value)


if __name__ == "__main__":
    unittest.main()
