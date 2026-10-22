from __future__ import annotations

from typing import Dict, Optional

from astreum.storage.radix.node import RadixNode


class RadixTree:
    def __init__(
        self,
        root_hash: Optional[bytes] = None,
        contact=None,
        root_block: Optional[bytes] = None,
    ) -> None:
        self.nodes: Dict[bytes, RadixNode] = {}
        self.root_hash = root_hash
        # ``RelayEndpoint`` of the one peer that holds this tree's
        # nodes (e.g. the validator of an uncommitted block). Every network
        # fetch for the tree goes there; ``None`` routes normally.
        self.contact = contact
        # The block whose record holds the root node, when the root is older
        # than the block building on it: its provider holds the root, and a
        # node reached by a plain hash is in the same block. None for a root
        # that is new, or a tree no block has recorded.
        self.root_block = root_block
