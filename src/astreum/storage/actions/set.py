from __future__ import annotations

import socket
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from ..models.atom import Atom


def _hot_storage_set(self, key: bytes, value: Atom) -> bool:
    """Store atom in hot storage without exceeding the configured limit."""
    node_logger = self.logger
    projected = self.hot_storage_size + value.size
    hot_limit = self.config["hot_storage_limit"]
    if projected > hot_limit:
        node_logger.warning(
            "Hot storage limit reached (%s > %s); skipping atom %s",
            projected,
            hot_limit,
            key.hex(),
        )
        return False

    self.hot_storage[key] = value
    self.hot_storage_size = projected
    node_logger.debug(
        "Stored atom %s in hot storage (bytes=%s, total=%s)",
        key.hex(),
        value.size,
        projected,
    )
    return True


def _cold_storage_set(self, key: bytes, atom: Atom) -> None:
    """Persist an atom into the cold storage directory if it already exists."""
    node_logger = self.logger
    atom_hex = key.hex()
    if not self.config["cold_storage_path"]:
        node_logger.debug("Cold storage disabled; skipping atom %s", atom_hex)
        return
    atom_bytes = atom.to_bytes()
    projected = self.cold_storage_size + len(atom_bytes)
    cold_limit = self.config["cold_storage_limit"]
    if cold_limit and projected > cold_limit:
        node_logger.warning(
            "Cold storage limit reached (%s > %s); skipping atom %s",
            projected,
            cold_limit,
            atom_hex,
        )
        return
    directory = Path(self.config["cold_storage_path"])
    if not directory.exists():
        node_logger.warning(
            "Cold storage path %s missing; skipping atom %s",
            directory,
            atom_hex,
        )
        return
    filename = f"{atom_hex.upper()}.bin"
    file_path = directory / filename
    try:
        file_path.write_bytes(atom_bytes)
        self.cold_storage_size = projected
        node_logger.debug("Persisted atom %s to cold storage", atom_hex)
    except OSError as exc:
        node_logger.error(
            "Failed writing atom %s to cold storage %s: %s",
            atom_hex,
            file_path,
            exc,
        )


def _network_set(self, atom_id: bytes) -> None:
    """Advertise an atom id to the closest known peer so they can fetch it from us."""
    node_logger = self.logger
    atom_hex = atom_id.hex()
    try:
        from ...communication.handlers.object_request import (
            ObjectRequest,
            ObjectRequestType,
        )
        from ...communication.models.message import Message, MessageTopic
    except Exception as exc:
        node_logger.warning(
            "Communication module unavailable; cannot advertise atom %s: %s",
            atom_hex,
            exc,
        )
        return

    try:
        provider_ip, provider_port = self.incoming_socket.getsockname()[:2]
    except Exception as exc:
        node_logger.warning(
            "Unable to determine provider address for atom %s: %s",
            atom_hex,
            exc,
        )
        return

    try:
        provider_ip_bytes = socket.inet_aton(provider_ip)
        provider_port_bytes = int(provider_port).to_bytes(2, "big", signed=False)
        provider_key_bytes = self.relay_public_key_bytes
    except Exception as exc:
        node_logger.warning("Unable to encode provider info for %s: %s", atom_hex, exc)
        return

    provider_payload = provider_key_bytes + provider_ip_bytes + provider_port_bytes

    try:
        closest_peer = self.peer_route.closest_peer_for_hash(atom_id)
    except Exception as exc:
        node_logger.warning("Peer lookup failed for atom %s: %s", atom_hex, exc)
        return

    is_self_closest = False
    if closest_peer is None or closest_peer.address is None:
        is_self_closest = True
    else:
        try:
            from ...communication.util import xor_distance
        except Exception as exc:
            node_logger.warning("Failed to import xor_distance for atom %s: %s", atom_hex, exc)
            is_self_closest = True
        else:
            try:
                self_distance = xor_distance(atom_id, self.relay_public_key_bytes)
                peer_distance = xor_distance(atom_id, closest_peer.public_key_bytes)
            except Exception as exc:
                node_logger.warning("Failed computing distance for atom %s: %s", atom_hex, exc)
                is_self_closest = True
            else:
                is_self_closest = self_distance <= peer_distance

    if is_self_closest:
        node_logger.debug("Self is closest; indexing provider for atom %s", atom_hex)
        self.storage_index[atom_id] = provider_payload
        return

    target_addr = closest_peer.address

    obj_req = ObjectRequest(
        type=ObjectRequestType.OBJECT_PUT,
        data=provider_payload,
        atom_id=atom_id,
    )
    
    message_body = obj_req.to_bytes()

    message = Message(
        topic=MessageTopic.OBJECT_REQUEST,
        content=message_body,
        sender=self.relay_public_key,
    )
    message.encrypt(closest_peer.shared_key_bytes)
    try:
        self.outgoing_queue.put((message.to_bytes(), target_addr))
        node_logger.debug(
            "Advertised atom %s to peer at %s:%s",
            atom_hex,
            target_addr[0],
            target_addr[1],
        )
    except Exception as exc:
        node_logger.error(
            "Failed to queue advertisement for atom %s to %s:%s: %s",
            atom_hex,
            target_addr[0],
            target_addr[1],
            exc,
        )
