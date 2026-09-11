from __future__ import annotations

import math
import time
from queue import Empty
from typing import Any, Callable

from astreum.consensus.account import create_account
from astreum.consensus.block.rate_window import update_statistics
from astreum.expression import Expr, link_list_to_expr, resolve_inner_exprs, resolve_list_exprs
from astreum.storage.exprs import put_expr_in_hot_storage
from astreum.storage.cold import put_expr_in_cold_storage
from astreum.storage.records import put_record_in_cold_storage
from astreum.consensus.models.block import Block
from astreum.consensus.block.create import create_block
from astreum.consensus.block.difficulty import calculate_block_difficulty
from astreum.consensus.block.nonce import generate_block_nonce
from astreum.consensus.block.encoding.decode import get_block_from_storage
from astreum.consensus.transaction import Transaction, apply_transaction_obj
from astreum.consensus.transaction.storage.initial import generate_initial_storage_record
from astreum.consensus.transaction.storage.pending import add_pending_storage_contract, finalize_pending_storage_contract
from astreum.consensus.transaction.storage.model import StorageRecord
from astreum.storage.radix import get_radix_node_expr, put_in_radix_tree
from astreum.storage.radix.node import radix_node_hash
from astreum.consensus.constants import STORAGE_ADDRESS, TREASURY_ADDRESS
from astreum.consensus.validation.validator import current_validator
from astreum.expression import ZERO32
from astreum.expression import RESOLUTION_RECORD
from astreum.storage.advertisements import advertise_exprs
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.ping import Ping
from astreum.communication.difficulty import message_difficulty
from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.storage.cold import put_expr_in_cold_storage
from astreum.crypto.bloom_tree import BloomTree
from astreum.consensus.models.accounts import extract_accounts_exprs
from astreum.crypto.bloom_search import make_search_variants, ERA_SIZE

validator_advertisment_limit_seconds = 15 * 60


def _process_trie_nodes(
    node: Any,
    block: Block,
    nodes: Any,
    items: Any,
) -> list[bytes]:
    """Generate storage records for a trie's nodes and return their storage ids.

    The returned ids are the trie keys written for each freshly generated
    record; already-registered nodes (``generate_initial_storage_record``
    returns ``None``) contribute nothing.
    """
    generated: list[bytes] = []
    temp_exprs: dict[bytes, Expr] = {h: get_radix_node_expr(n) for h, n in items}
    for n in nodes:
        result = generate_initial_storage_record(node, block, get_radix_node_expr(n), temp_exprs, mint=True)
        if result is None:
            continue
        record, slot_map, _, _ = result
        storage_id = radix_node_hash(n)
        storage_account = block.accounts.get_account(STORAGE_ADDRESS, node)
        if storage_account is not None:
            put_in_radix_tree(storage_account.data, node, storage_id, record.expr())
            for h, slot in slot_map.items():
                put_in_radix_tree(storage_account.data, node, h, slot.expr())
            storage_account.data_hash = storage_account.data.root_hash
        put_record_in_cold_storage(node, storage_id, list(slot_map.keys()))
        block.pending_exprs.append(record.expr())
        for slot in slot_map.values():
            block.pending_exprs.append(slot.expr())
        block.pending_exprs.append(get_radix_node_expr(n))
        generated.append(storage_id)
    return generated


def make_validation_worker(
    node: Any,
) -> Callable[[], None]:
    """Build the validation worker bound to the given node."""

    def _validation_worker() -> None:
        node.logger.info("Validation worker started")
        stop = node._validation_stop_event

        def _award_validator_reward(block: Block, reward_amount: int) -> None:
            if reward_amount <= 0:
                return
            accounts = getattr(block, "accounts", None)
            validator_key = getattr(block, "validator_public_key_bytes", None)
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
                validator_account = create_account()
            validator_account.balance += reward_amount
            accounts.set_account(validator_key, validator_account)

        while not stop.is_set():
            validation_public_key = node.config.get("validation_public_key_bytes")
            
            if not validation_public_key:
                node.logger.debug("Validation public key unavailable; sleeping")
                time.sleep(0.5)
                continue

            latest_block_hash = node.latest_block_hash
            if latest_block_hash is None:
                node.logger.warning("Missing latest_block_hash; retrying")
                time.sleep(0.5)
                continue

            node.logger.debug(
                "Querying current validator for block %s",
                latest_block_hash,
            )

            try:
                scheduled_validator, accounts_snapshot = current_validator(node, latest_block_hash)
            except Exception as exc:
                node.logger.exception("Unable to determine current validator: %s", exc)
                time.sleep(0.5)
                continue

            if scheduled_validator != validation_public_key:
                node.logger.debug("Current validator mismatch; expected %s", scheduled_validator.hex() if isinstance(scheduled_validator, bytes) else scheduled_validator)
                time.sleep(0.5)
                continue

            try:
                previous_block = get_block_from_storage(astreum_node=node, block_hash=latest_block_hash)
            except Exception:
                node.logger.exception("Unable to load previous block for validation")
                time.sleep(0.5)
                continue

            try:
                current_tx = node._validation_transaction_queue.get_nowait()
                queue_empty = False
            except Empty:
                current_tx = None
                queue_empty = True
                node.logger.debug(
                    "No pending validation transactions; generating empty block"
                )

            new_block = create_block(
                chain_id=getattr(node, "chain", 0),
                previous_block_hash=latest_block_hash,
                previous_block=previous_block,
                height=(previous_block.height or 0) + 1,
                timestamp=None,
                accounts_hash=previous_block.accounts_hash,
                bloom_hash=previous_block.bloom_hash,
                total_transaction_fee=0,
                total_storage_fee=0,
                transactions_hash=None,
                receipts_hash=None,
                difficulty=None,
                validator_public_key_bytes=validation_public_key,
                nonce=0,
                signature=None,
                accounts=accounts_snapshot,
                transactions=[],
                receipts=[],
            )
            node.logger.debug(
                "Creating block #%s extending %s",
                new_block.height,
                node.latest_block_hash,
            )

            new_block.transactions = new_block.transactions or []
            new_block.receipts = new_block.receipts or []

            storage_account = new_block.accounts.get_account(STORAGE_ADDRESS, node)
            if storage_account is not None:
                from astreum.consensus.block.encoding.expr import get_block_expr
                add_pending_storage_contract(node, new_block, None, None, get_block_expr(previous_block), mint=True)

            offset = new_block.height % ERA_SIZE
            if offset == 0 and new_block.height > 0:
                new_block.bloom_tree = BloomTree()
                new_block.previous_era_hash = previous_block.expr_id
            else:
                new_block.bloom_tree = BloomTree(new_block.bloom_hash)
                new_block.bloom_tree.set_leaf_start_hash(
                    previous_block.height % ERA_SIZE, previous_block.expr_id
                )

            while current_tx is not None:
                try:
                    apply_transaction_obj(node, new_block, current_tx)
                except NotImplementedError:
                    tx_hex = current_tx.hash
                    node.logger.warning("Transaction %s unsupported; re-queued", tx_hex)
                    node._validation_transaction_queue.put(current_tx)
                    time.sleep(0.5)
                    break
                except Exception:
                    tx_hex = current_tx.hash
                    node.logger.exception("Failed applying transaction %s", tx_hex)

                try:
                    current_tx = node._validation_transaction_queue.get_nowait()
                except Empty:
                    current_tx = None

            storage_ids: list[bytes] = []

            if new_block.pending_storage_contracts:
                contracts, _, refunds = finalize_pending_storage_contract(
                    node, new_block
                )
                storage_account = new_block.accounts.get_account(STORAGE_ADDRESS, node)
                if storage_account is not None:
                    for key, contract in contracts:
                        put_in_radix_tree(storage_account.data, node, key, contract.expr())
                        new_block.pending_exprs.append(contract.expr())
                        if isinstance(contract, StorageRecord):
                            storage_ids.append(key)
                    for trie_node in storage_account.data.nodes.values():
                        new_block.pending_exprs.append(get_radix_node_expr(trie_node))
                    storage_account.data_hash = storage_account.data.root_hash
                for sender_addr, refund_amount in refunds:
                    sender = new_block.accounts.get_account(sender_addr, node)
                    if sender is not None:
                        sender.balance += refund_amount
                        new_block.accounts.set_account(sender_addr, sender)

            total_transaction_fee = sum(r.transaction_fee for r in new_block.receipts)
            total_storage_fee = sum(r.storage_fee for r in new_block.receipts)
            total_mint = sum(r.mint for r in new_block.receipts)
            total_fee = sum(r.total_fee for r in new_block.receipts)

            new_block.total_transaction_fee = total_transaction_fee
            new_block.total_storage_fee = total_storage_fee

            treasury_account = new_block.accounts.get_account(TREASURY_ADDRESS, node)

            delta_fee = total_transaction_fee + total_storage_fee + new_block.total_mint
            delta_stake = treasury_account.balance
            new_block.statistics = update_statistics(
                new_block.height,
                getattr(previous_block, "statistics", None),
                delta_fee,
                delta_stake,
            )

            reward_amount = total_fee if total_fee > 0 else 1
            if total_fee == 0 and queue_empty:
                node.logger.debug("Awarding base validator reward of 1 aster")
            elif total_fee > 0:
                node.logger.debug(
                    "Collected %d aster in total fees for this block (tx=%d storage=%d)",
                    total_fee,
                    total_transaction_fee,
                    total_storage_fee,
                )
            _award_validator_reward(new_block, reward_amount)

            transactions = new_block.transactions or []
            tx_hashes = [tx.hash for tx in transactions if tx.hash]
            pending_exprs = list(new_block.pending_exprs)
            if tx_hashes:
                tx_list_expr = link_list_to_expr(tx_hashes)
                new_block.transactions_hash = tx_list_expr.hash()
                new_block.pending_exprs.append(tx_list_expr)
                pending_exprs.append(tx_list_expr)
            else:
                new_block.transactions_hash = ZERO32
            node.logger.debug("Block includes %d transactions", len(transactions))

            new_block.bloom_hash = (
                new_block.bloom_tree.root.expr().hash()
                if new_block.bloom_tree.root else ZERO32
            )

            pending_seen = {e.hash() for e in pending_exprs}
            for expr in new_block.accounts.pending_exprs:
                h = expr.hash()
                if h not in pending_seen:
                    new_block.pending_exprs.append(expr)
                    pending_exprs.append(expr)
                    pending_seen.add(h)

            new_block.receipts_hash = new_block.receipts_trie.root_hash if new_block.receipts_trie else ZERO32
            node.logger.debug("Block includes %d receipts", len(new_block.receipts or []))

            if new_block.receipts_trie is not None:
                for trie_node in new_block.receipts_trie.nodes.values():
                    new_block.pending_exprs.append(get_radix_node_expr(trie_node))
                    pending_exprs.append(get_radix_node_expr(trie_node))

            try:
                new_block.accounts_hash = new_block.accounts.update_trie(node) or ZERO32

                for trie_node in new_block.accounts._trie.nodes.values():
                    new_block.pending_exprs.append(get_radix_node_expr(trie_node))
                    pending_exprs.append(get_radix_node_expr(trie_node))

                for address, acct in new_block.accounts._cache.items():
                    if address == STORAGE_ADDRESS:
                        continue
                    storage_ids.extend(
                        _process_trie_nodes(node, new_block, acct.data.nodes.values(), acct.data.nodes.items())
                    )
                    storage_ids.extend(
                        _process_trie_nodes(node, new_block, acct.channels.nodes.values(), acct.channels.nodes.items())
                    )

                seen = {e.hash() for e in pending_exprs}
                for expr_item in extract_accounts_exprs(new_block.accounts):
                    h = expr_item.hash()
                    if h not in seen:
                        new_block.pending_exprs.append(expr_item)
                        pending_exprs.append(expr_item)
                        seen.add(h)

                node.logger.debug(
                    "Updated trie for %d cached accounts",
                    len(new_block.accounts._cache),
                )
            except Exception:
                node.logger.exception("Failed to update accounts trie for block")


            now = time.time()
            spacing = node.block_spacing
            min_allowed = new_block.previous_block.timestamp + spacing
            nonce_time_seconds = node.nonce_time_ms / 1000.0
            expected_blocktime = now + nonce_time_seconds + spacing
            new_block.timestamp = max(math.ceil(expected_blocktime), min_allowed)

            new_block.difficulty = calculate_block_difficulty(
                previous_timestamp=previous_block.timestamp,
                current_timestamp=new_block.timestamp,
                previous_difficulty=previous_block.difficulty,
            )
            
            try:
                nonce_started = time.perf_counter()
                generate_block_nonce(block=new_block, difficulty=previous_block.difficulty)
                elapsed_ms = int((time.perf_counter() - nonce_started) * 1000)
                setattr(node, "nonce_time_ms", elapsed_ms)
                node.logger.debug(
                    "Found nonce %s for block #%s at difficulty %s",
                    new_block.nonce,
                    new_block.height,
                    new_block.difficulty,
                )
            except Exception:
                node.logger.exception("Failed while searching for block nonce")
                time.sleep(0.5)
                continue
            
            now = time.time()
            if now > (new_block.timestamp + 2):
                node.logger.warning(
                    "Skipping block #%s propagation; timestamp %s already elapsed (now=%s)",
                    new_block.height,
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
                
            from astreum.consensus.block.encoding.expr import get_block_expr
            new_block_hash = get_block_expr(new_block).hash()
            hot_store_failures = 0

            if not put_expr_in_hot_storage(node, get_block_expr(new_block)):
                hot_store_failures += 1

            for pending_expr in pending_exprs:
                if not put_expr_in_hot_storage(node, pending_expr):
                    hot_store_failures += 1

            if hot_store_failures:
                node.logger.warning(
                    "Block hot storage writes skipped for block #%s: count=%s",
                    new_block.height,
                    hot_store_failures,
                )

            put_expr_in_cold_storage(node, get_block_expr(new_block))

            expires_at = time.time() + validator_advertisment_limit_seconds
            advertisement_ids = list(dict.fromkeys([new_block_hash, *storage_ids]))
            if advertisement_ids:
                entries = [
                    (expr_id, RESOLUTION_RECORD, expires_at)
                    for expr_id in advertisement_ids
                ]
                advertised_ids, advertise_warning = advertise_exprs(node, entries=entries)
                if advertise_warning:
                    node.logger.warning(
                        "Block advertisement batch had failures for block #%s: advertised=%s reason=%s",
                        new_block.height,
                        len(advertised_ids),
                        advertise_warning,
                    )
            node.logger.info(
                "Created block #%s with hash %s",
                new_block.height,
                new_block_hash.hex(),
            )

            put_expr_in_cold_storage(node, get_block_expr(new_block))

            for pending_expr in pending_exprs:
                put_expr_in_cold_storage(node, pending_expr)

            if node.outgoing_queue and node.peers:
                try:
                    with node.peers_lock:
                        peers = list(node.peers.items())
                except Exception:
                    peers = list(node.peers.items())

                if peers:
                    ping_payload = Ping(
                        is_validator=True,
                        difficulty=message_difficulty(node),
                        latest_block=new_block_hash,
                    ).to_bytes()

                    for peer_key, peer in peers:
                        peer_hex = (
                            peer_key.hex()
                            if isinstance(peer_key, (bytes, bytearray))
                            else peer_key
                        )
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
                                sender_public_key_bytes=node.storage_public_key_bytes,
                            )
                            ping_msg.encrypt(peer.shared_key_bytes)
                            if enqueue_outgoing(
                                node,
                                address,
                                message=ping_msg,
                                difficulty=peer.difficulty,
                            ):
                                node.logger.debug(
                                    "Queued validator ping to %s (%s)",
                                    address,
                                    peer_key.hex()
                                    if isinstance(peer_key, (bytes, bytearray))
                                    else peer_key,
                                )
                            else:
                                node.logger.debug(
                                    "Dropped validator ping to %s (%s); enqueue rejected",
                                    address,
                                    peer_key.hex()
                                    if isinstance(peer_key, (bytes, bytearray))
                                    else peer_key,
                                )
                        except Exception:
                            node.logger.exception("Failed queueing validator ping to %s", address)

            with node.latest_block_lock:
                node.latest_block_hash = new_block_hash
                node.latest_block = new_block

        node.logger.info("Validation worker stopped")

    return _validation_worker
