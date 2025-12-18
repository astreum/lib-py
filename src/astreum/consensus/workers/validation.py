from __future__ import annotations

import math
import time
from queue import Empty
from typing import Any, Callable

from ..models.account import Account
from ..models.accounts import Accounts
from ..models.block import Block
from ..models.transaction import apply_transaction
from ..validator import current_validator
from ...storage.models.atom import bytes_list_to_atoms
from ...communication.models.message import Message, MessageTopic
from ...communication.models.ping import Ping


def make_validation_worker(
    node: Any,
) -> Callable[[], None]:
    """Build the validation worker bound to the given node."""

    def _validation_worker() -> None:
        node.logger.info("Validation worker started")
        stop = node._validation_stop_event

        def _award_validator_reward(block: Block, reward_amount: int) -> None:
            """Credit the validator account with the provided reward."""
            if reward_amount <= 0:
                return
            accounts = getattr(block, "accounts", None)
            validator_key = getattr(block, "validator_public_key", None)
            if accounts is None or not validator_key:
                node.logger.debug(
                    "Skipping validator reward; accounts snapshot or key missing"
                )
                return
            try:
                validator_account = accounts.get_account(
                    address=validator_key, node=node
                )
            except Exception:
                node.logger.exception("Unable to load validator account for reward")
                return
            if validator_account is None:
                validator_account = Account.create()
            validator_account.balance += reward_amount
            accounts.set_account(validator_key, validator_account)

        while not stop.is_set():
            validation_public_key = getattr(node, "validation_public_key", None)
            if not validation_public_key:
                node.logger.debug("Validation public key unavailable; sleeping")
                time.sleep(0.5)
                continue

            latest_block_hash = getattr(node, "latest_block_hash", None)
            if not isinstance(latest_block_hash, (bytes, bytearray)):
                node.logger.warning("Missing latest_block_hash; retrying")
                time.sleep(0.5)
                continue

            node.logger.debug(
                "Querying current validator for block %s",
                latest_block_hash.hex()
                if isinstance(latest_block_hash, (bytes, bytearray))
                else latest_block_hash,
            )
            try:
                scheduled_validator, _ = current_validator(node, latest_block_hash)
            except Exception as exc:
                node.logger.exception("Unable to determine current validator: %s", exc)
                time.sleep(0.5)
                continue

            if scheduled_validator != validation_public_key:
                expected_hex = (
                    scheduled_validator.hex()
                    if isinstance(scheduled_validator, (bytes, bytearray))
                    else scheduled_validator
                )
                node.logger.debug("Current validator mismatch; expected %s", expected_hex)
                time.sleep(0.5)
                continue

            try:
                previous_block = Block.from_atom(node, latest_block_hash)
            except Exception:
                node.logger.exception("Unable to load previous block for validation")
                time.sleep(0.5)
                continue

            try:
                current_hash = node._validation_transaction_queue.get_nowait()
                queue_empty = False
            except Empty:
                current_hash = None
                queue_empty = True
                node.logger.debug(
                    "No pending validation transactions; generating empty block"
                )

            try:
                accounts_snapshot = Accounts(root_hash=previous_block.accounts_hash)
            except Exception:
                accounts_snapshot = None
                node.logger.warning("Unable to initialise accounts snapshot for block")

            new_block = Block(
                chain_id=getattr(node, "chain", 0),
                previous_block_hash=latest_block_hash,
                previous_block=previous_block,
                number=(previous_block.number or 0) + 1,
                timestamp=None,
                accounts_hash=previous_block.accounts_hash,
                transactions_total_fees=0,
                transactions_hash=None,
                receipts_hash=None,
                delay_difficulty=None,
                validator_public_key=validation_public_key,
                nonce=0,
                signature=None,
                accounts=accounts_snapshot,
                transactions=[],
                receipts=[],
            )
            node.logger.debug(
                "Creating block #%s extending %s",
                new_block.number,
                (
                    node.latest_block_hash.hex()
                    if isinstance(node.latest_block_hash, (bytes, bytearray))
                    else node.latest_block_hash
                ),
            )

            # we may want to add a timer to process part of the txs only on a slow computer
            total_fees = 0
            while current_hash is not None:
                try:
                    total_fees += apply_transaction(node, new_block, current_hash)
                except NotImplementedError:
                    tx_hex = (
                        current_hash.hex()
                        if isinstance(current_hash, (bytes, bytearray))
                        else current_hash
                    )
                    node.logger.warning("Transaction %s unsupported; re-queued", tx_hex)
                    node._validation_transaction_queue.put(current_hash)
                    time.sleep(0.5)
                    break
                except Exception:
                    tx_hex = (
                        current_hash.hex()
                        if isinstance(current_hash, (bytes, bytearray))
                        else current_hash
                    )
                    node.logger.exception("Failed applying transaction %s", tx_hex)

                try:
                    current_hash = node._validation_transaction_queue.get_nowait()
                except Empty:
                    current_hash = None

            new_block.transactions_total_fees = total_fees
            reward_amount = total_fees if total_fees > 0 else 1
            if total_fees == 0 and queue_empty:
                node.logger.debug("Awarding base validator reward of 1 aster")
            elif total_fees > 0:
                node.logger.debug(
                    "Collected %d aster in transaction fees for this block", total_fees
                )
            _award_validator_reward(new_block, reward_amount)

            # create an atom list of transactions, save the list head hash as the block's transactions_hash
            transactions = new_block.transactions or []
            tx_hashes = [bytes(tx.hash) for tx in transactions if tx.hash]
            head_hash, _ = bytes_list_to_atoms(tx_hashes)
            new_block.transactions_hash = head_hash
            node.logger.debug("Block includes %d transactions", len(transactions))

            receipts = new_block.receipts or []
            receipt_atoms = []
            receipt_hashes = []
            for rcpt in receipts:
                receipt_id, atoms = rcpt.to_atom()
                receipt_atoms.extend(atoms)
                receipt_hashes.append(bytes(receipt_id))
            receipts_head, _ = bytes_list_to_atoms(receipt_hashes)
            new_block.receipts_hash = receipts_head
            node.logger.debug("Block includes %d receipts", len(receipts))

            account_atoms = []
            if new_block.accounts is not None:
                try:
                    account_atoms = new_block.accounts.update_trie(node)
                    new_block.accounts_hash = new_block.accounts.root_hash
                    node.logger.debug(
                        "Updated trie for %d cached accounts",
                        len(new_block.accounts._cache),
                    )
                except Exception:
                    node.logger.exception("Failed to update accounts trie for block")

            now = time.time()
            min_allowed = new_block.previous_block.timestamp + 1
            nonce_time_seconds = node.nonce_time_ms / 1000.0
            expected_blocktime = now + nonce_time_seconds
            new_block.timestamp = max(int(math.ceil(expected_blocktime)), min_allowed)

            new_block.delay_difficulty = Block.calculate_delay_difficulty(
                previous_timestamp=previous_block.timestamp,
                current_timestamp=new_block.timestamp,
                previous_difficulty=previous_block.delay_difficulty,
            )
            
            try:
                nonce_started = time.perf_counter()
                new_block.generate_nonce(difficulty=previous_block.delay_difficulty)
                elapsed_ms = int((time.perf_counter() - nonce_started) * 1000)
                setattr(node, "nonce_time_ms", elapsed_ms)
                node.logger.debug(
                    "Found nonce %s for block #%s at difficulty %s",
                    new_block.nonce,
                    new_block.number,
                    new_block.delay_difficulty,
                )
            except Exception:
                node.logger.exception("Failed while searching for block nonce")
                time.sleep(0.5)
                continue
            
            # wait until the block timestamp is reached before propagating
            now = time.time()
            if now > new_block.timestamp:
                node.logger.warning(
                    "Skipping block #%s propagation; timestamp %s already elapsed (now=%s)",
                    new_block.number,
                    new_block.timestamp,
                    now,
                )
                continue

            spread_delay = new_block.timestamp - now
            if spread_delay > 0:
                node.logger.debug(
                    "Delaying distribution for %.3fs to reach block timestamp %s",
                    spread_delay,
                    new_block.timestamp,
                )
                time.sleep(spread_delay)
                
            # atomize block
            new_block_hash, new_block_atoms = new_block.to_atom()
            # put as own latest block hash
            node.latest_block_hash = new_block_hash
            node.latest_block = new_block
            node.logger.info(
                "Created block #%s with hash %s (%d atoms)",
                new_block.number,
                new_block_hash.hex(),
                len(new_block_atoms),
            )
            

            # ping peers in the validation route to update their records
            if node.validation_route and node.outgoing_queue and node.peers:
                route_peers = {
                    peer_key
                    for bucket in getattr(node.validation_route, "buckets", {}).values()
                    for peer_key in bucket
                }
                if route_peers:
                    ping_payload = Ping(
                        is_validator=True,
                        latest_block=new_block_hash,
                    ).to_bytes()

                    for peer_key in route_peers:
                        peer_hex = (
                            peer_key.hex()
                            if isinstance(peer_key, (bytes, bytearray))
                            else peer_key
                        )
                        peer = node.get_peer(peer_key)
                        if peer is None:
                            node.logger.debug(
                                "Skipping validator ping to peer %s; peer not found",
                                peer_hex,
                            )
                            continue
                        address = getattr(peer, "address", None)
                        if not address:
                            node.logger.debug(
                                "Skipping validator ping to %s; address missing",
                                peer_hex,
                            )
                            continue
                        try:
                            ping_msg = Message(
                                topic=MessageTopic.PING,
                                content=ping_payload,
                                sender=node.relay_public_key,
                            )
                            ping_msg.encrypt(peer.shared_key_bytes)
                            node.outgoing_queue.put((ping_msg.to_bytes(), address))
                            node.logger.debug(
                                "Queued validator ping to %s (%s)",
                                address,
                                peer_key.hex()
                                if isinstance(peer_key, (bytes, bytearray))
                                else peer_key,
                            )
                        except Exception:
                            node.logger.exception("Failed queueing validator ping to %s", address)

            # upload block atoms
            for block_atom in new_block_atoms:
                atom_id = block_atom.object_id()
                node._hot_storage_set(key=atom_id, value=block_atom)

            # upload receipt atoms
            for receipt_atom in receipt_atoms:
                atom_id = receipt_atom.object_id()
                node._hot_storage_set(key=atom_id, value=receipt_atom)

            # upload account atoms
            for account_atom in account_atoms:
                atom_id = account_atom.object_id()
                node._hot_storage_set(key=atom_id, value=account_atom)

        node.logger.info("Validation worker stopped")

    return _validation_worker
