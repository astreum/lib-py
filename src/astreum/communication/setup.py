import socket, threading, time
from queue import Queue
from typing import Tuple, Optional, Set
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from astreum import Node

from astreum.communication import Route, Message
from astreum.communication.processors.incoming import (
    process_incoming_messages,
    populate_incoming_messages,
)
from astreum.communication.processors.outgoing import process_outgoing_messages
from astreum.communication.processors.peer import manage_peer
from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.communication.util import address_str_to_host_and_port
from astreum.storage.workers.advertisements import advertise_storage
from astreum.storage.workers.claim import claim_storage
from astreum.storage.workers.requests import init_request_buffer, request_storage
from astreum.utils.bytes import hex_to_bytes
from astreum.utils.config import DEFAULT_SEED

def load_x25519(hex_key: Optional[str]) -> X25519PrivateKey:
    """DH key for relaying (always X25519)."""
    if hex_key:
        return X25519PrivateKey.from_private_bytes(bytes.fromhex(hex_key))
    return X25519PrivateKey.generate()

def make_routes(
    storage_pk_bytes: bytes,
    val_sk: Optional[ed25519.Ed25519PrivateKey]
) -> Tuple[Route, Optional[Route]]:
    """Peer route (Ed25519 storage key) + optional validation route (ed pubkey)."""
    peer_rt = Route(storage_pk_bytes)
    val_rt  = Route(val_sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )) if val_sk else None
    return peer_rt, val_rt

def make_maps():
    """Empty lookup maps: peers and addresses."""
    return


def _resolve_default_seed_ips(node: "Node", default_seed: Optional[str]) -> Set[str]:
    if default_seed is None:
        return set()
    try:
        host, port = address_str_to_host_and_port(default_seed)
    except Exception as exc:
        node.logger.warning("Invalid default seed %s: %s", default_seed, exc)
        return set()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    except Exception as exc:
        node.logger.warning("Failed resolving default seed %s:%s: %s", host, port, exc)
        return set()
    resolved = {info[4][0] for info in infos if info[4]}
    if resolved:
        resolved_list = ", ".join(sorted(resolved))
        node.logger.info("Default seed resolved to %s", resolved_list)
    else:
        node.logger.warning("No IPs resolved for default seed %s:%s", host, port)
    return resolved


def _resolve_relay_ip_address(node: "Node") -> Optional[str]:
    try:
        host, port = address_str_to_host_and_port(DEFAULT_SEED)
    except Exception as exc:
        node.logger.warning("Invalid default seed %s: %s", DEFAULT_SEED, exc)
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((host, port))
            local_ip = sock.getsockname()[0]
            socket.inet_aton(local_ip)
            return local_ip
    except Exception as exc:
        node.logger.debug("Failed deriving relay IP via default seed: %s", exc)
    return None


def communication_setup(node: "Node", config: dict):
    node.logger.info("Setting up node communication")
    node.use_ipv6              = config.get('use_ipv6', False)
    node.peers_lock = threading.RLock()
    node.communication_stop_event = threading.Event()
    default_seed = config.get("default_seed")
    node.default_seed_ips = _resolve_default_seed_ips(node, default_seed)
    node.relay_ip_address = _resolve_relay_ip_address(node)

    # key loading
    node.relay_secret_key      = load_x25519(config.get('relay_secret_key'))
    node.storage_secret_key    = config["storage_secret_key"]
    node.storage_public_key    = config["storage_public_key"]
    node.storage_public_key_bytes = config["storage_public_key_bytes"]

    # derive pubs + routes
    node.relay_public_key      = node.relay_secret_key.public_key()
    node.relay_public_key_bytes = node.relay_public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    node.config["relay_public_key"] = node.relay_public_key
    node.config["relay_public_key_bytes"] = node.relay_public_key_bytes
    node.peer_route, node.validation_route = make_routes(
        node.storage_public_key_bytes,
        node.config.get("validation_secret_key")
    )

    # connection state & atom request tracking
    node.is_connected = False
    node.expr_requests = {}
    node.expr_requests_lock = threading.RLock()
    init_request_buffer(node)

    # sockets + queues + threads
    with node.peers_lock:
        node.peers = {}


    port = config.get("port")
    if port is None:
        raise ValueError("port must be configured before communication setup")
    fam = socket.AF_INET6 if node.use_ipv6 else socket.AF_INET
    node.socket = socket.socket(fam, socket.SOCK_DGRAM)
    if node.use_ipv6:
        node.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    node.socket.bind(("::" if node.use_ipv6 else "0.0.0.0", port))
    bound_port = node.socket.getsockname()[1]
    if port != 0 and bound_port != port:
        raise OSError(
            f"port mismatch: requested {port}, got {bound_port}"
        )
    node.config["port"] = bound_port if port == 0 else port
    node.socket.settimeout(0.5)
    node.logger.info(
        "Incoming UDP socket bound to %s:%s",
        "::" if node.use_ipv6 else "0.0.0.0",
        node.config["port"],
    )
    node.incoming_queue = Queue()
    node.incoming_queue_size = 0
    node.incoming_queue_size_lock = threading.RLock()
    node.incoming_queue_size_limit = node.config.get("incoming_queue_size_limit", 0)
    node.incoming_queue_timeout = node.config.get("incoming_queue_timeout", 0)
    node.incoming_populate_thread = threading.Thread(
        target=populate_incoming_messages,
        args=(node,),
        daemon=True,
    )
    node.incoming_process_thread = threading.Thread(
        target=process_incoming_messages,
        args=(node,),
        daemon=True,
    )
    node.incoming_populate_thread.start()
    node.incoming_process_thread.start()

    node.outgoing_queue = Queue()

    node.outgoing_thread = threading.Thread(
        target=process_outgoing_messages,
        args=(node,),
        daemon=True,
    )
    node.outgoing_thread.start()

    node.peer_manager_thread  = threading.Thread(
        target=manage_peer,
        args=(node,),
        daemon=True
    )
    node.peer_manager_thread.start()

    latest_block_hex = config.get("latest_block_hash")
    if latest_block_hex:
        try:
            node.latest_block_hash = hex_to_bytes(latest_block_hex, expected_length=32)
        except Exception as exc:
            node.logger.warning("Invalid latest_block_hash in config: %s", exc)
            node.latest_block_hash = None
    # else: preserve the existing instance attribute — don't wipe it.
    # communication_setup runs on every node.connect() and should only
    # overwrite latest_block_hash when config explicitly provides one.

    node.logger.info(
        "Communication ready (port=%s, bootstrap_count=%s)",
        node.config["port"],
        len(node.bootstrap_peers),
    )
    node.is_connected = True

    # bootstrap pings (requires connected state for enqueue_outgoing)
    for addr in node.bootstrap_peers:
        try:
            host, port = address_str_to_host_and_port(addr)  # type: ignore[arg-type]
        except Exception as exc:
            node.logger.warning("Invalid bootstrap address %s: %s", addr, exc)
            continue

        handshake_message = Message(
            handshake=True,
            sender_public_key_bytes=node.storage_public_key_bytes,
            content=node.relay_public_key_bytes,
        )
        enqueue_outgoing(
            node,
            (host, port),
            message=handshake_message,
            difficulty=1,
        )
        node.logger.info("Sent bootstrap handshake to %s:%s", host, port)
    if node.bootstrap_peers:
        node._bootstrap_last_attempt = time.time()

    try:
        node.storage_request_current_price = max(
            0,
            int(node.config.get("storage_request_minimum_price", 0) or 0),
        )
    except Exception:
        node.storage_request_current_price = 0
    
    node.advertise_storage_thread = threading.Thread(
        target=advertise_storage,
        args=(node,),
        daemon=True,
    )
    node.advertise_storage_thread.start()

    node.request_storage_thread = threading.Thread(
        target=request_storage,
        args=(node,),
        daemon=True,
    )
    node.request_storage_thread.start()

    node.claim_storage_thread = threading.Thread(
        target=claim_storage,
        args=(node,),
        daemon=True,
    )
    node.claim_storage_thread.start()
