import threading
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives import serialization
from datetime import datetime, timezone
from typing import Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from astreum.node import Node

class Peer:
    def __init__(
        self,
        node_secret_key: X25519PrivateKey,
        peer_public_key: X25519PublicKey,
        storage_public_key_bytes: bytes,
        latest_block: Optional[bytes] = None,
        address: Optional[Tuple[str, int]] = None,
        is_default_seed: bool = False,
        difficulty: int = 1,
        handshake_timestamp: int = 0,
    ):
        self.shared_key_bytes = node_secret_key.exchange(peer_public_key)
        self.timestamp = datetime.now(timezone.utc)
        self.latest_block = latest_block
        self.difficulty = max(1, difficulty or 1)
        self.address = address
        self.is_default_seed = bool(is_default_seed)
        # Ed25519 storage key = routing identity (XOR distance, buckets)
        self.public_key_bytes = storage_public_key_bytes
        # X25519 relay key = transport encryption (raw bytes for DH)
        self.relay_public_key_bytes = peer_public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        # Timestamp of the signed handshake that set the relay key
        self.handshake_timestamp = handshake_timestamp
        # Traffic-sharing counters are updated by multiple communication threads.
        self.metrics_lock = threading.RLock()
        self.total_network_upload = 0
        self.total_network_download = 0
        self.shared_storage_upload = 0
        self.shared_storage_download = 0

    def set_relay_key(
        self,
        node_secret_key: X25519PrivateKey,
        relay_key_bytes: bytes,
        handshake_timestamp: int,
    ) -> None:
        """Replace the relay key and the shared key of this peer in place.

        The peer object stays the same, so the route and the metrics keep
        their reference to it.

        Args:
            node_secret_key (X25519PrivateKey): Relay secret key of this node.
            relay_key_bytes (bytes): New X25519 relay public key (32 bytes).
            handshake_timestamp (int): Timestamp of the handshake that carried
                the new relay key.

        Raises:
            ValueError: If the relay key is not a valid X25519 public key.
        """
        peer_public_key = X25519PublicKey.from_public_bytes(relay_key_bytes)
        self.shared_key_bytes = node_secret_key.exchange(peer_public_key)
        self.relay_public_key_bytes = relay_key_bytes
        self.handshake_timestamp = handshake_timestamp


def increment_peer_metric(peer: "Peer", attr_name: str, amount: int) -> None:
    """Increment a peer metric counter under the peer metrics lock."""
    try:
        delta = amount
    except Exception:
        return
    if delta <= 0:
        return

    lock = getattr(peer, "metrics_lock", None)
    if lock is None:
        current = int(getattr(peer, attr_name, 0))
        setattr(peer, attr_name, current + delta)
        return

    with lock:
        current = int(getattr(peer, attr_name, 0))
        setattr(peer, attr_name, current + delta)


def add_peer(node: "Node", peer_key, peer: "Peer") -> "Peer":
    """Register a peer entry on the node under lock."""
    with node.peers_lock:
        node.peers[peer_key] = peer
    return peer


def replace_peer(node: "Node", old_key, peer_key, peer: "Peer") -> "Peer":
    """Replace an existing peer entry (if any) with a new one under lock."""
    with node.peers_lock:
        node.peers.pop(old_key, None)
        node.peers[peer_key] = peer
    return peer


def get_peer(node: "Node", peer_key):
    """Retrieve a peer entry under lock."""
    with node.peers_lock:
        return node.peers.get(peer_key)


def remove_peer(node: "Node", peer_key):
    """Remove a peer entry under lock."""
    with node.peers_lock:
        return node.peers.pop(peer_key, None)
