"""Walk to the branch of this node in the network.

Each round asks the known peer nearest to this node's own key for the nodes it
knows nearest to that key. The reply makes this node send handshakes, and each
handshake puts a node in ``node.peer_route``. The walk ends when no nearer peer
comes in, or when ``self_lookup_rounds`` rounds are sent.

``SelfLookupTimer`` repeats the walk every ``self_lookup_interval`` seconds. Each
completed walk also refreshes one far bucket of the route.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.route import Route
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


@dataclass
class SelfLookupTimer:
    """State of the repeating walk. The caller keeps it between steps.

    Attributes:
        walk: The walk in progress, or the last completed walk.
        joined: True after the first walk is complete.
        next_walk_at: Monotonic start time of the next walk, or None.
        cursor: Bucket index where the next far-bucket refresh starts.
    """

    walk: SelfLookup = field(default_factory=SelfLookup)
    joined: bool = False
    next_walk_at: float | None = None
    cursor: int = 0


def _send_self_route_request(
    node: "Node", peer: "Peer", target: bytes | None = None
) -> bool:
    """Send a ``ROUTE_REQUEST`` for the peer route to a peer.

    Args:
        node: The node that asks.
        peer: The peer to ask.
        target: Key for the peer to rank its reply by, or None to rank by the
            key of this node.

    Returns:
        True if the request was queued, False otherwise.
    """
    request = Message(
        topic=MessageTopic.ROUTE_REQUEST,
        content=bytes([ROUTE_ID_PEERS]) + (target or b""),
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


def _random_target_in_bucket(own: bytes, index: int) -> bytes:
    """Return a random key that shares exactly ``index`` leading bits with own.

    Args:
        own: The key of this node.
        index: Number of leading bits to share. Bit ``index`` is flipped.

    Returns:
        A key of the same length as ``own``.
    """
    bits = len(own) * 8
    own_int = int.from_bytes(own, "big")
    random_int = int.from_bytes(os.urandom(len(own)), "big")
    keep_mask = ((1 << index) - 1) << (bits - index)
    flip_bit = 1 << (bits - 1 - index)
    value = (
        (own_int & keep_mask)
        | ((own_int & flip_bit) ^ flip_bit)
        | (random_int & (flip_bit - 1))
    )
    return value.to_bytes(len(own), "big")


def refresh_one_bucket(node: "Node", cursor: int) -> int:
    """Ask for nodes in one far bucket that is not full.

    Only buckets farther from this node than the nearest known peer are
    candidates. Buckets nearer than that peer are empty. The request goes to
    the known peer nearest to a random key in the bucket.

    Args:
        node: The node that asks.
        cursor: Bucket index to start from.

    Returns:
        The cursor for the next refresh.
    """
    own = node.storage_public_key_bytes
    route = node.peer_route
    nearest = route.closest_peer_for_hash(own)
    if nearest is None:
        return cursor
    limit = Route._matching_leading_bits(own, nearest.public_key_bytes)
    candidates = [i for i in range(limit) if len(route.buckets[i]) < route.bucket_size]
    if not candidates:
        return cursor
    index = next((i for i in candidates if i >= cursor), candidates[0])
    target = _random_target_in_bucket(own, index)
    peer = route.closest_peer_for_hash(target)
    if peer is None or peer.address is None:
        return index + 1
    _send_self_route_request(node, peer, target)
    return index + 1


def self_lookup_timer_step(node: "Node", timer: SelfLookupTimer, now: float) -> bool:
    """Run one step of the repeating walk.

    Args:
        node: The node that walks.
        timer: The state of the timer. The step changes it.
        now: The current monotonic time in seconds.

    Returns:
        True after the first walk is complete, False before.
    """
    if node.config["self_lookup_rounds"] == 0:
        timer.joined = True
        return True
    was_done = timer.walk.done
    done = self_lookup_step(node, timer.walk, now)
    if done:
        timer.joined = True
    if done and not was_done:
        timer.cursor = refresh_one_bucket(node, timer.cursor)
        interval = node.config["self_lookup_interval"]
        timer.next_walk_at = now + interval if interval > 0 else None
    elif done and timer.next_walk_at is not None and now >= timer.next_walk_at:
        timer.walk = SelfLookup()
        timer.next_walk_at = None
        self_lookup_step(node, timer.walk, now)
    return timer.joined
