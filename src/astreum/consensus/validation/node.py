import threading
from queue import Queue

from astreum.communication.node import connect_node
from astreum.consensus.fork.node import fork_setup
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from astreum.utils.bytes import hex_to_bytes
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.ping import Ping
from astreum.communication.difficulty import message_difficulty
from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.consensus.validation.genesis import create_genesis_block
from astreum.consensus.block.encoding.decode import get_block_from_storage
from astreum.consensus.validation.worker import make_validation_worker
from astreum.consensus.verification.node import verify_blockchain
from astreum.expression import resolve_inner_exprs
from astreum.storage.exprs import put_expr_in_hot_storage
from astreum.storage.cold import put_expr_in_cold_storage
from astreum.consensus.models.accounts import extract_accounts_exprs


def validate_blockchain(self, validation_secret_key: Ed25519PrivateKey):
    connect_node(self)
    fork_setup(self)

    default_seed = self.config.get("default_seed")
    if not default_seed:
        verify_blockchain(self)
    else:
        self.logger.info(
            "Skipping verification; default_seed configured as trusted head provider"
        )

    self.logger.info("Setting up node consensus")

    latest_block_hex = self.config.get("latest_block_hash")
    if latest_block_hex is not None:
        self.latest_block_hash = hex_to_bytes(latest_block_hex, expected_length=32)
        try:
            self.latest_block = get_block_from_storage(astreum_node=self, block_hash=self.latest_block_hash)
        except Exception:
            self.logger.warning(
                "Failed to load latest block %s from storage; will rebuild",
                self.latest_block_hash.hex(),
            )
            self.latest_block_hash = None
            self.latest_block = None

    self.nonce_time_ms = 0
    self.block_spacing = 2
    
    self.logger.info(
        "Consensus latest_block_hash preset: %s",
        self.latest_block_hash,
    )

    from astreum.consensus.validation.ads import init_pending_ads

    init_pending_ads(self)
    self._validation_transaction_queue = Queue()
    self._validation_stop_event = threading.Event()

    def enqueue_transaction_hash(tx_hash: bytes) -> None:
        if not isinstance(tx_hash, (bytes, bytearray)):
            raise TypeError("transaction hash must be bytes-like")
        from astreum.consensus.transaction.from_storage import get_transaction_from_storage
        transaction = get_transaction_from_storage(self, bytes(tx_hash))
        self._validation_transaction_queue.put(transaction)

    self.enqueue_transaction_hash = enqueue_transaction_hash

    validation_worker = make_validation_worker(self)

    self.consensus_validation_thread = threading.Thread(
        target=validation_worker, daemon=True, name="consensus-validation"
    )
    self.logger.info(
        "Consensus validation worker prepared (%s)",
        self.consensus_validation_thread.name,
    )

    self.logger.info(
        "Initializing block and transaction processing for chain %s",
        self.config["chain"],
    )

    self.config["validation_secret_key"] = validation_secret_key
    validation_public_key_obj = self.config["validation_secret_key"].public_key()
    validation_public_key_bytes = validation_public_key_obj.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    self.config["validation_public_key"] = validation_public_key_obj
    self.config["validation_public_key_bytes"] = validation_public_key_bytes
    self.logger.debug(
        "Derived validator public key %s", validation_public_key_bytes.hex()
    )

    if self.latest_block_hash is None:
        genesis_block = create_genesis_block(
            self,
            validator_public_key=validation_public_key_bytes,
            chain_id=self.config["chain_id"],
        )
        from astreum.consensus.block.encoding.expr import get_block_expr
        genesis_hash = get_block_expr(genesis_block).hash()
        genesis_exprs, _ = resolve_inner_exprs(self, get_block_expr(genesis_block))
        self.logger.debug(
            "Genesis block created with %s exprs",
            len(genesis_exprs),
        )
        genesis_hot_store_failures = 0

        for expr_item in genesis_exprs:
            try:
                if not put_expr_in_hot_storage(self, expr_item):
                    genesis_hot_store_failures += 1
            except Exception as exc:
                self.logger.warning(
                    "Unable to persist genesis expr %s: %s",
                    expr_item.hash(),
                    exc,
                )

        if genesis_block.accounts is not None:
            for expr_item in extract_accounts_exprs(genesis_block.accounts):
                try:
                    if not put_expr_in_hot_storage(self, expr_item):
                        genesis_hot_store_failures += 1
                except Exception as exc:
                    self.logger.warning(
                        "Unable to persist accounts expr %s: %s",
                        expr_item.hash(),
                        exc,
                    )

        if genesis_block.accounts_hash is not None:
            from astreum.consensus.models.accounts import Accounts
            from astreum.consensus.constants import TREASURY_ADDRESS
            verify_accounts = Accounts(root_hash=genesis_block.accounts_hash)
            if verify_accounts.get_account(TREASURY_ADDRESS, self) is None:
                raise ValueError(
                    "genesis sanity check: treasury account not findable in accounts trie"
                )

        if genesis_hot_store_failures:
            self.logger.warning(
                "Genesis hot storage writes skipped: count=%s",
                genesis_hot_store_failures,
            )

        for expr_item in genesis_exprs:
            put_expr_in_cold_storage(self, expr_item)
        if genesis_block.accounts is not None:
            for expr_item in extract_accounts_exprs(genesis_block.accounts):
                put_expr_in_cold_storage(self, expr_item)

        self.latest_block_hash = genesis_hash
        self.latest_block = genesis_block
        self.logger.info("Genesis block stored with hash %s", genesis_hash.hex())
    else:
        self.logger.debug(
            "latest_block_hash already set to %s; skipping genesis creation",
            self.latest_block_hash.hex()
            if isinstance(self.latest_block_hash, (bytes, bytearray))
            else self.latest_block_hash,
        )

    validation_thread = self.consensus_validation_thread
    if validation_thread.is_alive():
        self.logger.debug("Consensus validation thread already running")
    else:
        self.logger.info(
            "Starting consensus validation thread (%s)",
            validation_thread.name,
        )
        validation_thread.start()

    try:
        ping_payload = Ping(
            is_validator=bool(self.config.get("validation_public_key_bytes")),
            difficulty=message_difficulty(self),
            latest_block=self.latest_block_hash,
        ).to_bytes()
    except Exception as exc:
        self.logger.debug("Failed to build validation ping payload: %s", exc)
        return

    if self.outgoing_queue and self.peers:
        with self.peers_lock:
            peers = list(self.peers.items())
        for peer_key, peer in peers:
            peer_hex = (
                peer_key.hex()
                if isinstance(peer_key, (bytes, bytearray))
                else peer_key
            )
            address = peer.address
            if not address:
                self.logger.debug(
                    "Skipping validation ping to %s; address missing",
                    peer_hex,
                )
                continue
            try:
                ping_msg = Message(
                    topic=MessageTopic.PING,
                    content=ping_payload,
                    sender_public_key_bytes=self.storage_public_key_bytes,
                )
                ping_msg.encrypt(peer.shared_key_bytes)
                queued = enqueue_outgoing(
                    self,
                    address,
                    message=ping_msg,
                    difficulty=peer.difficulty,
                )
                if queued:
                    self.logger.debug(
                        "Queued validation ping to %s (%s)",
                        address,
                        peer_hex,
                    )
                else:
                    self.logger.debug(
                        "Dropped validation ping to %s (%s)",
                        address,
                        peer_hex,
                    )
            except Exception:
                self.logger.exception(
                    "Failed queueing validation ping to %s",
                    address,
                )
