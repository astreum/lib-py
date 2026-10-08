from __future__ import annotations

from typing import Any, Dict

from astreum.communication.models.peer import get_peer


def validator_peers(node: Any) -> Dict[bytes, Any]:
    """Connected validator peers (with an address), keyed by public key."""

    Args:
        node (Any): Description.

    validation_route = getattr(node, "validation_route", None)
    if validation_route is None:
        return {}
    validators: Dict[bytes, Any] = {}
    for peer in list(validation_route.peers.values()):
        if peer is not None and getattr(peer, "address", None):
            validators[getattr(peer, "public_key_bytes", None)] = peer
    for bucket in list(validation_route.buckets.values()):
        for peer_key in bucket:
            peer = get_peer(node, peer_key)
            if peer is not None and getattr(peer, "address", None):
                validators[peer.public_key_bytes] = peer
    return validators
