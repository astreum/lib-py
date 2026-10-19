from itertools import chain, islice
from typing import Dict, Iterable, List, Optional
from astreum.communication.models.peer import Peer
from astreum.communication.util import xor_distance


class Route:
    def __init__(self, storage_public_key_bytes: bytes, bucket_size: int = 16):
        self.storage_public_key_bytes = storage_public_key_bytes
        self.bucket_size = bucket_size
        self.buckets: Dict[int, List[bytes]] = {
            i: [] for i in range(len(self.storage_public_key_bytes) * 8)
        }
        self.peers: Dict[bytes, Peer] = {}

    @staticmethod
    def _matching_leading_bits(a: bytes, b: bytes) -> int:
        for byte_index, (ba, bb) in enumerate(zip(a, b)):
            diff = ba ^ bb
            if diff:
                return byte_index * 8 + (8 - diff.bit_length())
        return len(a) * 8

    def _normalize_peer_key(self, peer_public_key_bytes: bytes) -> bytes:
        key_bytes = peer_public_key_bytes
        if len(key_bytes) != len(self.storage_public_key_bytes):
            raise ValueError("peer key must be raw 32-byte public key")
        return key_bytes

    def add_peer(self, peer_public_key_bytes: bytes, peer: Optional[Peer] = None):
        peer_public_key_bytes = self._normalize_peer_key(peer_public_key_bytes)
        if peer_public_key_bytes == self.storage_public_key_bytes:
            return
        bucket_idx = self._matching_leading_bits(self.storage_public_key_bytes, peer_public_key_bytes)
        if len(self.buckets[bucket_idx]) < self.bucket_size:
            bucket = self.buckets[bucket_idx]
            if peer_public_key_bytes not in bucket:
                bucket.append(peer_public_key_bytes)
        if peer is not None:
            self.peers[peer_public_key_bytes] = peer

    def remove_peer(self, peer_public_key_bytes: bytes):
        peer_public_key_bytes = self._normalize_peer_key(peer_public_key_bytes)
        if peer_public_key_bytes == self.storage_public_key_bytes:
            return
        bucket_idx = self._matching_leading_bits(self.storage_public_key_bytes, peer_public_key_bytes)
        bucket = self.buckets.get(bucket_idx)
        if not bucket:
            return
        try:
            bucket.remove(peer_public_key_bytes)
        except ValueError:
            pass
        self.peers.pop(peer_public_key_bytes, None)

    def closest_peer_for_hash(self, target_hash: bytes) -> Optional[Peer]:
        """Return the peer with the minimal XOR distance to ``target_hash``."""
        if not isinstance(target_hash, (bytes, bytearray)):
            raise TypeError("target_hash must be bytes-like")

        target = target_hash
        if len(target) != len(self.storage_public_key_bytes):
            raise ValueError("target_hash must match peer key length (32 bytes)")

        closest_key: Optional[bytes] = None
        closest_distance: Optional[int] = None

        for bucket in self.buckets.values():
            for peer_key in bucket:
                try:
                    distance = xor_distance(target, peer_key)
                except ValueError:
                    continue
                if closest_distance is None or distance < closest_distance:
                    closest_distance = distance
                    closest_key = peer_key

        if closest_key is None:
            return None
        peer = self.peers.get(closest_key)
        return peer

    def count_nearer(
        self, target: bytes, key: bytes, limit: int, extra: Iterable[bytes] = ()
    ) -> int:
        """Count the known keys that are nearer to ``target`` than ``key``.

        The count is the place of ``key`` for ``target``: 0 is the nearest
        node. The route never holds its own key, so a caller that must count
        this node passes the key in ``extra``. A key counts one time. ``key``
        itself never counts.

        Args:
            target: The 32-byte hash that the distance is measured to.
            key: The 32-byte key whose place is counted.
            limit: The count stops at this value.
            extra: More keys to count, for example listed providers.

        Returns:
            The number of nearer keys, at most ``limit``.
        """
        distance = xor_distance(target, key)
        seen: set[bytes] = {key}
        count = 0
        keys = (peer_key for bucket in self.buckets.values() for peer_key in bucket)
        for other in chain(keys, extra):
            if other in seen:
                continue
            seen.add(other)
            try:
                nearer = xor_distance(target, other) < distance
            except ValueError:
                continue
            if nearer:
                count += 1
                if count >= limit:
                    return count
        return count

    def closest_peers(self, target: bytes, count: int) -> List[Peer]:
        """Return the peers nearest to ``target``, nearest first.

        Args:
            target: The 32-byte hash that the distance is measured to.
            count: The largest number of peers to return.

        Returns:
            Up to ``count`` peers. A key with no ``Peer`` object is skipped.
        """
        keys = [peer_key for bucket in self.buckets.values() for peer_key in bucket]
        keys.sort(key=lambda peer_key: xor_distance(target, peer_key))
        peers = (self.peers.get(peer_key) for peer_key in keys)
        return list(islice((p for p in peers if p is not None), count))
