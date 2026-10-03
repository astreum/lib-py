from __future__ import annotations

from typing import Dict, Optional

from astreum.storage.radix.node import RadixNode


class RadixTree:
    def __init__(
        self,
        root_hash: Optional[bytes] = None,
        contact=None,
    ) -> None:
        self.nodes: Dict[bytes, RadixNode] = {}
        self.root_hash = root_hash
        # ``(relay_key, address, port)`` of the one peer that holds this tree's
        # nodes (e.g. the validator of an uncommitted block). Every network
        # fetch for the tree goes there; ``None`` routes normally.
        self.contact = contact
