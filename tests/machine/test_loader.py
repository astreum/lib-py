import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.machine import Expr, assemble_env

SCRIPTS = Path(__file__).resolve().parent / "loader_scripts"


class TestResolve(unittest.TestCase):
    def test_plain_def(self):
        env = assemble_env(
            node=None, script=str(SCRIPTS / "math_sum.aex"), target="calc_sum",
        )
        self.assertIsNotNone(env.get("calc_sum"))

    def test_imported_def_dotted_target(self):
        env = assemble_env(
            node=None, script=str(SCRIPTS / "main.aex"), target="math.calc_sum",
        )
        self.assertIsNotNone(env.get("math.calc_sum"))

    def test_transitive_dep_via_body_ref(self):
        env = assemble_env(
            node=None, script=str(SCRIPTS / "multi.aex"), target="a.add_one",
        )
        self.assertIsNotNone(env.get("a.add_one"))
        self.assertIsNotNone(env.get("a.s.foo"))
        self.assertIsNone(env.get("b.sub_one"))

    def test_transitive_dep_other_branch(self):
        env = assemble_env(
            node=None, script=str(SCRIPTS / "multi.aex"), target="b.sub_one",
        )
        self.assertIsNotNone(env.get("b.sub_one"))
        self.assertIsNotNone(env.get("b.s.foo"))
        self.assertIsNone(env.get("a.add_one"))

    def test_dotted_target_through_nested_import(self):
        env = assemble_env(
            node=None, script=str(SCRIPTS / "multi.aex"), target="a.s.foo",
        )
        self.assertIsNotNone(env.get("a.s.foo"))
        self.assertIsNone(env.get("a.add_one"))
        self.assertIsNone(env.get("b.sub_one"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
