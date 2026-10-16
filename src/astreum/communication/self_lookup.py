"""Walk to the branch of this node in the network.

Each round asks the known peer nearest to this node's own key for the nodes it
knows nearest to that key. The reply makes this node send handshakes, and each
handshake puts a node in ``node.peer_route``. The walk ends when no nearer peer
comes in, or when ``self_lookup_rounds`` rounds are sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.communication.util import xor_distance

if TYPE_CHECKING:
    from astreum.communication.models.peer import Peer
    from astreum.node import Node

ROUTE_ID_PEERS = 0


@dataclass
class SelfLookup:
    """State of one walk. The caller keeps it between steps.

    Attributes:
        rounds: Number of rounds sent.
        distance: XOR distance of the peer asked last, or None before round 1.
        deadline: Monotonic end time of the last round, or None before round 1.
        done: True when the walk is complete.
    """

    rounds: int = 0
    distance: int | None = None
    deadline: float | None = None
    done: bool = False


def _send_self_route_request(node: "Node", peer: "Peer") -> bool:
    """Send a ``ROUTE_REQUEST`` for the peer route to a peer.

    Args:
        node: The node that asks.
        peer: The peer to ask.

    Returns:
        True if the request was queued, False otherwise.
    """
    request = Message(
        topic=MessageTopic.ROUTE_REQUEST,
        content=bytes([ROUTE_ID_PEERS]),
        sender_public_key_bytes=node.storage_public_key_bytes,
    )
    request.encrypt(peer.shared_key_bytes)
    return enqueue_outgoing(
        node,
        peer.address,
        message=request,
        difficulty=peer.difficulty,
    )


def self_lookup_step(node: "Node", walk: SelfLookup, now: float) -> bool:
    """Run one step of the walk.

    A step sends the next request when a peer nearer than the last peer asked
    is in the route and the round cap is not reached. The walk is complete when
    no nearer peer is in the route at the deadline of the last round, or when a
    nearer peer comes in after the last allowed round.

    Args:
        node: The node that walks.
        walk: The state of the walk. The step changes it.
        now: The current monotonic time in seconds.

    Returns:
        True if the walk is complete, False otherwise.
    """
    if walk.done:
        return True
    cap = node.config["self_lookup_rounds"]
    if cap == 0:
        walk.done = True
        return True

    own = node.storage_public_key_bytes
    peer = node.peer_route.closest_peer_for_hash(own)
    distance = None
    nearer = False
    if peer is not None and peer.address is not None:
        distance = xor_distance(own, peer.public_key_bytes)
        nearer = walk.distance is None or distance < walk.distance

    if nearer and walk.rounds < cap:
        if _send_self_route_request(node, peer):
            walk.rounds += 1
            walk.distance = distance
            walk.deadline = now + node.config["peer_timeout_interval"]
        return False
    if walk.deadline is not None and (nearer or now >= walk.deadline):
        walk.done = True
        return True
    return False
