"""Tests for the Merkle tree module."""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from blake3 import blake3

from astreum.expression import ZERO32
from astreum.crypto.merkle import (
    MerkleTree,
    create_merkle_tree,
    generate_merkle_tree_proof,
    verify_merkle_tree_proof,
)


def value(i: int) -> bytes:
    return blake3(i.to_bytes(4, "big")).digest()


def values(n: int) -> list:
    return [value(i) for i in range(n)]


def leaf(v: bytes) -> bytes:
    return blake3(b"\x00" + v).digest()


def node(l: bytes, r: bytes) -> bytes:
    return blake3(b"\x01" + l + r).digest()


def reference_root(vs: list) -> bytes:
    """Recursive RFC 6962 root, independent of the module."""
    if not vs:
        return ZERO32
    if len(vs) == 1:
        return leaf(vs[0])
    k = 1
    while k * 2 < len(vs):
        k *= 2
    return node(reference_root(vs[:k]), reference_root(vs[k:]))


FIXED_ROOTS = {
    0: ZERO32.hex(),
    1: "566cb2dc801c118804c7b43398e34846f8118bfec86dcd416d1e379bd3e0d1b5",
    2: "ea3457a4d9cc65a3c938dbfab9e17ed7f79d8c25d9439365ff8ef5c941b0f6e0",
    3: "a281220d459756ef772622ccdcb6a998c1f83f009a9ce76fd9e1868d4db08516",
    4: "2ab79a127e9c23b4f47a2c28c13521ba297463ed35837d19f65c2f89076479f3",
    5: "559599fd252b8dcb3b8298b202e1707affab5b5af5f5a199eac9b1dfc1efdac1",
    7: "f8daa15306843f6be3e001f393e0aace2fe1a18c4f70a73bb77bb2b6b1d4a981",
    8: "8696733dae6ebb84db8318f653d344719fdba0739dd127aa9152df3b96084243",
    1000: "e837533dc2d263a835fbeffdf929d1629f0880b9d4d5b525e4bf6853897789f0",
}


class TestRoots(unittest.TestCase):
    def test_fixed_vectors(self):
        for n, expected in FIXED_ROOTS.items():
            self.assertEqual(create_merkle_tree(values(n)).root.hex(), expected, n)

    def test_matches_reference(self):
        for n in range(0, 41):
            self.assertEqual(create_merkle_tree(values(n)).root, reference_root(values(n)), n)

    def test_empty_tree(self):
        t = create_merkle_tree([])
        self.assertEqual(t.root, ZERO32)
        self.assertEqual(t.count, 0)
        self.assertEqual(t.levels, [])

    def test_domain_separation(self):
        a, b, c, d = values(4)
        self.assertNotEqual(
            create_merkle_tree([a, b, c, d]).root,
            create_merkle_tree([node(leaf(a), leaf(b)), node(leaf(c), leaf(d))]).root,
        )
        self.assertNotEqual(leaf(a + b), node(a, b))


class TestProofs(unittest.TestCase):
    def test_roundtrip_all_indices(self):
        for n in range(1, 41):
            vs = values(n)
            t = create_merkle_tree(vs)
            for i in range(n):
                proof = generate_merkle_tree_proof(t, i)
                self.assertTrue(verify_merkle_tree_proof(t, i, vs[i], proof), (n, i))
                # sibling count: levels where the node has a sibling
                pos, count = i, 0
                for lv in t.levels[:-1]:
                    if (pos ^ 1) < len(lv):
                        count += 1
                    pos >>= 1
                self.assertEqual(len(proof), count, (n, i))

    def test_root_only_tree_verifies(self):
        vs = values(13)
        full = create_merkle_tree(vs)
        bare = MerkleTree(full.root, full.count)
        self.assertIsNone(bare.levels)
        for i in range(13):
            self.assertTrue(
                verify_merkle_tree_proof(bare, i, vs[i], generate_merkle_tree_proof(full, i))
            )
        with self.assertRaises(ValueError):
            generate_merkle_tree_proof(bare, 0)

    def test_failures(self):
        vs = values(11)
        t = create_merkle_tree(vs)
        i = 6
        proof = generate_merkle_tree_proof(t, i)
        self.assertTrue(verify_merkle_tree_proof(t, i, vs[i], proof))

        self.assertFalse(verify_merkle_tree_proof(t, i, vs[i + 1], proof))  # wrong value
        self.assertFalse(verify_merkle_tree_proof(t, i + 1, vs[i], proof))  # wrong index
        self.assertFalse(verify_merkle_tree_proof(t, i, vs[i], proof[:-1]))  # too few
        self.assertFalse(verify_merkle_tree_proof(t, i, vs[i], proof + [value(99)]))  # too many
        self.assertFalse(verify_merkle_tree_proof(t, i, vs[i], []))

        flipped = list(proof)
        flipped[0] = bytes([flipped[0][0] ^ 1]) + flipped[0][1:]
        self.assertFalse(verify_merkle_tree_proof(t, i, vs[i], flipped))

        self.assertFalse(verify_merkle_tree_proof(t, 11, vs[0], proof))  # index >= count
        self.assertFalse(verify_merkle_tree_proof(t, -1, vs[0], proof))
        self.assertFalse(verify_merkle_tree_proof(t, i, vs[i][:31], proof))  # short value
        self.assertFalse(verify_merkle_tree_proof(t, i, vs[i] + b"\x00", proof))
        self.assertFalse(verify_merkle_tree_proof(t, i, vs[i], proof[:-1] + [proof[-1] + b"\x00"]))

        other = create_merkle_tree(values(12)[::-1])
        self.assertFalse(verify_merkle_tree_proof(other, i, vs[i], proof))  # other tree

    def test_empty_tree_rejects(self):
        t = create_merkle_tree([])
        self.assertFalse(verify_merkle_tree_proof(t, 0, value(0), []))

    def test_wrong_count_changing_layout_fails(self):
        vs = values(5)
        t = create_merkle_tree(vs)
        proof = generate_merkle_tree_proof(t, 0)
        self.assertTrue(verify_merkle_tree_proof(t, 0, vs[0], proof))
        self.assertFalse(verify_merkle_tree_proof(MerkleTree(t.root, 2), 0, vs[0], proof))

    def test_count_is_not_bound(self):
        """Documents the trust rule: the root does not commit to the count."""
        vs = values(5)
        t = create_merkle_tree(vs)
        proof = generate_merkle_tree_proof(t, 4)
        self.assertTrue(verify_merkle_tree_proof(MerkleTree(t.root, 3), 2, vs[4], proof))
        self.assertTrue(
            verify_merkle_tree_proof(
                MerkleTree(t.root, 6), 0, vs[0], generate_merkle_tree_proof(t, 0)
            )
        )

    def test_input_checks(self):
        with self.assertRaises(ValueError):
            create_merkle_tree([b"short"])
        with self.assertRaises(ValueError):
            create_merkle_tree([value(0), value(1) + b"\x00"])
        t = create_merkle_tree(values(3))
        with self.assertRaises(ValueError):
            generate_merkle_tree_proof(t, -1)
        with self.assertRaises(ValueError):
            generate_merkle_tree_proof(t, 3)

    def test_depth_at_164076(self):
        n = 164_076
        t = create_merkle_tree(values(n))
        self.assertEqual(len(t.levels), 19)
        for i in (0, n - 1):
            proof = generate_merkle_tree_proof(t, i)
            self.assertLessEqual(len(proof), 18)
            self.assertTrue(verify_merkle_tree_proof(t, i, value(i), proof))


if __name__ == "__main__":
    unittest.main()
