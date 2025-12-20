import threading
from queue import Queue

from astreum.communication.node import connect_node
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from astreum.utils.bytes import hex_to_bytes
from astreum.validation.genesis import create_genesis_block
from astreum.validation.workers import make_validation_worker
from astreum.verification.node import verify_blockchain


def validate_blockchain(self, validator_secret_key: Ed25519PrivateKey):
    """Initialize validator keys, ensure genesis exists, then start validation thread."""
    connect_node(self)

    verify_blockchain(self)

    self.logger.info("Setting up node consensus")

    latest_block_hex = self.config.get("latest_block_hash")
    if latest_block_hex is not None:
        self.latest_block_hash = hex_to_bytes(latest_block_hex, expected_length=32)

    self.latest_block_hash = getattr(self, "latest_block_hash", None)
    self.latest_block = getattr(self, "latest_block", None)
    self.nonce_time_ms = getattr(self, "nonce_time_ms", 0)
    
    self.logger.info(
        "Consensus latest_block_hash preset: %s",
        self.latest_block_hash.hex()
        if isinstance(self.latest_block_hash, (bytes, bytearray))
        else self.latest_block_hash,
    )

    self._validation_transaction_queue = getattr(
        self, "_validation_transaction_queue", Queue()
    )
    self._validation_stop_event = getattr(
        self, "_validation_stop_event", threading.Event()
    )

    def enqueue_transaction_hash(tx_hash: bytes) -> None:
        """Schedule a transaction hash for validation processing."""
        if not isinstance(tx_hash, (bytes, bytearray)):
            raise TypeError("transaction hash must be bytes-like")
        self._validation_transaction_queue.put(bytes(tx_hash))

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

    self.validation_secret_key = validator_secret_key
    validator_public_key_obj = self.validation_secret_key.public_key()
    validator_public_key_bytes = validator_public_key_obj.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    self.validation_public_key = validator_public_key_bytes
    self.logger.debug(
        "Derived validator public key %s", validator_public_key_bytes.hex()
    )

    if self.latest_block_hash is None:
        genesis_block = create_genesis_block(
            self,
            validator_public_key=validator_public_key_bytes,
            chain_id=self.config["chain_id"],
        )
        account_atoms = genesis_block.accounts.update_trie(self) if genesis_block.accounts else []

        genesis_hash, genesis_atoms = genesis_block.to_atom()
        self.logger.debug(
            "Genesis block created with %s atoms (%s account atoms)",
            len(genesis_atoms),
            len(account_atoms),
        )

        for atom in account_atoms + genesis_atoms:
            try:
                self._hot_storage_set(key=atom.object_id(), value=atom)
            except Exception as exc:
                self.logger.warning(
                    "Unable to persist genesis atom %s: %s",
                    atom.object_id(),
                    exc,
                )

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

    validation_thread = getattr(self, "consensus_validation_thread", None)
    if validation_thread is None:
        raise RuntimeError("Consensus validation not initialized; connect the node first.")

    if validation_thread.is_alive():
        self.logger.debug("Consensus validation thread already running")
    else:
        self.logger.info(
            "Starting consensus validation thread (%s)",
            validation_thread.name,
        )
        validation_thread.start()

    # ping all peers to announce validation capability
