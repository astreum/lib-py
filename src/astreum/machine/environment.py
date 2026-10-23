from astreum.expression import Expr, NIL, link, symbol
from typing import Dict, Optional


class Env:
    def __init__(self, data: Dict[str, Expr] = None, parent: "Env" = None):
        """Initialize an environment.

        Args:
            data: Initial variable bindings.
            parent: Parent environment for lexical scoping.

        Returns: None
        """
        self.data: Dict[str, Expr] = {} if data is None else data
        self.parent = parent

    def get(self, key: str) -> Optional[Expr]:
        """Look up a variable in the environment chain.

        Args:
            key: The variable name to look up.

        Returns:
            The bound expression, or None if not found.
        """
        if key in self.data:
            return self.data[key]
        if self.parent:
            return self.parent.get(key=key)
        return None

    def put(self, key: str, value: Expr):
        """Bind a variable in the current environment.

        Args:
            key: The variable name.
            value: The expression to bind.

        Returns: None
        """
        self.data[key] = value


def env_to_radix_tree(env: Env, node, parent_hash: bytes = None) -> Expr:
    """Convert an environment to a radix tree expression.

    Args:
        env: The environment to convert.
        node: The Astreum Node instance for storage access.
        parent_hash: Hash of the parent environment level.

    Returns:
        An expression representing the environment as a radix tree.
    """
    """Env level as a tagged expr: link(level . env), built over the level's
    bindings via the Patricia trie.

    The bindings go into a RadixTree (key = symbol UTF-8 bytes, value = bound
    Expr; insertion order irrelevant — the trie canonicalizes). level =
    Expr("link", head_hash=parent_hash, tail=bindings_root_expr): the head
    contributes parent_hash DIRECTLY to the level's hash (via get_expr_head_hash),
    so the chain hash composes parent-with-bindings with no wrapping, and the
    parent sits OUTSIDE the bindings key space. Root env: head is NIL.

    The returned expr's .hash() is the chain hash. The trie nodes are the
    expr's transitive content — persist via put_expr_in_hot_storage, which
    recurses head/tail hashes.
    """
    from astreum.storage.radix import RadixTree, put_in_radix_tree
    from astreum.storage.radix.node import get_radix_node_expr

    tree = RadixTree()
    for key, value in env.data.items():
        put_in_radix_tree(tree, node, key.encode("utf-8"), value)
    bindings_root_expr = (
        get_radix_node_expr(tree.nodes[tree.root_hash])
        if tree.root_hash
        else NIL
    )
    if parent_hash:
        level = Expr("link", head_hash=parent_hash, tail=bindings_root_expr)
    else:
        level = link(NIL, bindings_root_expr)
    return link(level, symbol("env"))
