from __future__ import annotations

from typing import Any, List, Optional

from astreum.expression import Expr, link_list_to_expr
from astreum.storage.exprs import get_expr_list
from astreum.consensus.block.rate_window import update_statistics
from astreum.storage.radix import RadixTree, get_from_radix_tree, put_in_radix_tree
from astreum.consensus.models.accounts import Accounts

ZERO32 = b"\x00" * 32
from astreum.consensus.models.receipt import Receipt
from astreum.consensus.block.encoding.expr import get_block_expr
from astreum.consensus.transaction import apply_transaction
from astreum.consensus.transaction.storage.initial import generate_initial_storage_record
from astreum.consensus.account import create_account
from astreum.consensus.constants import STORAGE_ADDRESS, TREASURY_ADDRESS


def _hex(value: Optional[bytes]) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def verify_block_transactions(node: Any, block: Any) -> tuple[bool, Optional[str]]:
    """Verify receipts, transactions, and accounts hashes for this block."""
    if node is None:
        raise ValueError("node required for block verification")

    node.logger.debug("Block verify start block=%s", _hex(block.expr_id))

    if block.transactions_hash is None:
        node.logger.debug(
            "Block verify missing transactions_hash block=%s",
            _hex(block.expr_id),
        )
        return False, "missing transactions_hash"
    if block.receipts_hash is None:
        node.logger.debug(
            "Block verify missing receipts_hash block=%s",
            _hex(block.expr_id),
        )
        return False, "missing receipts_hash"
    if block.accounts_hash is None:
        node.logger.debug(
            "Block verify missing accounts_hash block=%s",
            _hex(block.expr_id),
        )
        return False, "missing accounts_hash"

    def _load_hash_list(head: bytes) -> Optional[List[bytes]]:
        if head == ZERO32:
            return []
        expr = get_expr_list(node, head)
        if expr is None:
            node.logger.debug(
                "Block verify missing list expr head=%s block=%s",
                _hex(head),
                _hex(block.expr_id),
            )
            return None
        result = []
        current = expr
        while current._tag == "link":
            if current._head_hash is None:
                node.logger.debug(
                    "Block verify list node missing head_hash head=%s block=%s",
                    _hex(head),
                    _hex(block.expr_id),
                )
                return None
            result.append(current._head_hash)
            current = current._tail
        return result

    prev_hash = block.previous_block_hash or ZERO32
    if prev_hash == ZERO32:
        if block.transactions_hash != ZERO32:
            node.logger.debug(
                "Block verify genesis tx hash mismatch block=%s",
                _hex(block.expr_id),
            )
            return False, "genesis tx hash mismatch"
        if block.receipts_hash != ZERO32:
            node.logger.debug(
                "Block verify genesis receipts hash mismatch block=%s",
                _hex(block.expr_id),
            )
            return False, "genesis receipts hash mismatch"
        if block.total_transaction_fee not in (0, None):
            node.logger.debug(
                "Block verify genesis total transaction fee mismatch block=%s",
                _hex(block.expr_id),
            )
            return False, "genesis total transaction fee mismatch"
        if block.total_storage_fee not in (0, None):
            node.logger.debug(
                "Block verify genesis total storage fee mismatch block=%s",
                _hex(block.expr_id),
            )
            return False, "genesis total storage fee mismatch"
        if block.total_fee != 0:
            node.logger.debug(
                "Block verify genesis total fee mismatch block=%s",
                _hex(block.expr_id),
            )
            return False, "genesis total fee mismatch"
        if (
            getattr(block, "global_loaned", 0)
            or getattr(block, "global_defaulted", 0)
            or getattr(block, "global_loan_count", 0)
        ):
            node.logger.debug(
                "Block verify genesis global credit totals mismatch block=%s",
                _hex(block.expr_id),
            )
            return False, "genesis global credit totals mismatch"
        if block.accounts_hash is None:
            node.logger.debug(
                "Block verify genesis missing accounts hash block=%s",
                _hex(block.expr_id),
            )
            return False, "genesis missing accounts hash"
        genesis_accounts = Accounts(root_hash=block.accounts_hash)
        treasury_account = genesis_accounts.get_account(TREASURY_ADDRESS, node)
        if treasury_account is None:
            node.logger.debug(
                "Block verify genesis missing treasury account block=%s",
                _hex(block.expr_id),
            )
            return False, "genesis missing treasury account"
        storage_account = genesis_accounts.get_account(STORAGE_ADDRESS, node)
        if storage_account is None:
            node.logger.debug(
                "Block verify genesis missing storage account block=%s",
                _hex(block.expr_id),
            )
            return False, "genesis missing storage account"
        expected_stats = [(1, treasury_account.balance or 0, 0, 0)]
        if getattr(block, "statistics", None) != expected_stats:
            node.logger.debug(
                "Block verify genesis statistics mismatch block=%s",
                _hex(block.expr_id),
            )
            return False, "genesis statistics mismatch"
        node.logger.debug("Block verify genesis passed block=%s", _hex(block.expr_id))
        return True, None

    prev_block = block.previous_block
    if prev_block is None:
        node.logger.debug(
            "Block verify failed loading parent block=%s prev=%s",
            _hex(block.expr_id),
            _hex(prev_hash),
        )
        return False, "failed loading parent block"

    if not prev_block.accounts_hash:
        node.logger.debug(
            "Block verify missing parent accounts hash block=%s",
            _hex(block.expr_id),
        )
        return False, "missing parent accounts hash"

    tx_hashes = _load_hash_list(block.transactions_hash)
    if tx_hashes is None:
        return False, "failed loading tx list"

    expected_tx_head = link_list_to_expr(tx_hashes).hash()
    if expected_tx_head != (block.transactions_hash or ZERO32):
        node.logger.debug(
            "Block verify tx head mismatch block=%s expected=%s actual=%s",
            _hex(block.expr_id),
            _hex(expected_tx_head),
            _hex(block.transactions_hash),
        )
        return False, "tx head mismatch"

    accounts_snapshot = Accounts(root_hash=prev_block.accounts_hash)
    work_block = type("_WorkBlock", (), {})()
    work_block.chain_id = block.chain_id
    work_block.previous_block_hash = block.previous_block_hash
    work_block.previous_block = prev_block
    work_block.height = block.height
    work_block.accounts = accounts_snapshot
    work_block.transactions = []
    work_block.receipts = []
    work_block.receipts_trie = None
    work_block.total_mint = 0
    work_block.pending_exprs = []
    work_block.global_loaned = getattr(prev_block, "global_loaned", 0) or 0
    work_block.global_defaulted = getattr(prev_block, "global_defaulted", 0) or 0
    work_block.global_loan_count = getattr(prev_block, "global_loan_count", 0) or 0

    # Pre-commit previous block expr to storage data (replicating block builder)
    storage_account = work_block.accounts.get_account(STORAGE_ADDRESS, node)
    if storage_account is not None:
        result = generate_initial_storage_record(
            node, work_block, get_block_expr(prev_block), mint=True
        )
        if result is not None:
            record, slot_map, _, _ = result
            put_in_radix_tree(storage_account.data, node, prev_block.expr_id, record.expr())
            storage_account.data_hash = storage_account.data.root_hash
            for h, slot in slot_map.items():
                put_in_radix_tree(storage_account.data, node, h, slot.expr())
            storage_account.data_hash = storage_account.data.root_hash

    for tx_hash in tx_hashes:
        apply_transaction(node, work_block, tx_hash)

    # Derive totals from collected receipts
    total_transaction_fee = sum(r.transaction_fee for r in work_block.receipts)
    total_storage_fee = sum(r.storage_fee for r in work_block.receipts)
    total_fee = sum(r.total_fee for r in work_block.receipts)

    # Award validator reward (replicating block builder)
    reward_amount = total_fee if total_fee > 0 else 1
    validator_key = getattr(block, "validator_public_key_bytes", None)
    if validator_key:
        try:
            validator_account = work_block.accounts.get_account(
                address=validator_key, node=node
            )
        except Exception:
            node.logger.exception("Unable to load validator account for reward")
            validator_account = None
        if validator_account is None:
            validator_account = create_account()
        validator_account.balance += reward_amount
        work_block.accounts.set_account(validator_key, validator_account)

    # Set start_hash on the previous era leaf and verify bloom hash
    era_offset = block.height % 1024
    if era_offset > 0 and prev_block is not None and block.bloom_tree is not None:
        block.bloom_tree.set_leaf_start_hash(era_offset - 1, prev_block.expr_id)
    expected_bloom = (
        block.bloom_tree.root.expr().hash()
        if block.bloom_tree is not None and block.bloom_tree.root
        else ZERO32
    )
    if block.bloom_hash != expected_bloom:
        node.logger.debug(
            "Block verify bloom hash mismatch block=%s expected=%s actual=%s",
            _hex(block.expr_id), _hex(expected_bloom), _hex(block.bloom_hash),
        )
        return False, "bloom hash mismatch"

    if block.total_transaction_fee is None:
        node.logger.debug(
            "Block verify missing total transaction fee block=%s",
            _hex(block.expr_id),
        )
        return False, "missing total transaction fee"
    if block.total_transaction_fee != total_transaction_fee:
        node.logger.debug(
            "Block verify total transaction fee mismatch block=%s expected=%s actual=%s",
            _hex(block.expr_id),
            total_transaction_fee,
            block.total_transaction_fee,
        )
        return False, "total transaction fee mismatch"
    if block.total_storage_fee is None:
        node.logger.debug(
            "Block verify missing total storage fee block=%s",
            _hex(block.expr_id),
        )
        return False, "missing total storage fee"
    if block.total_storage_fee != total_storage_fee:
        node.logger.debug(
            "Block verify total storage fee mismatch block=%s expected=%s actual=%s",
            _hex(block.expr_id),
            total_storage_fee,
            block.total_storage_fee,
        )
        return False, "total storage fee mismatch"
    if block.total_fee != total_fee:
        node.logger.debug(
            "Block verify total fee mismatch block=%s expected=%s actual=%s",
            _hex(block.expr_id),
            total_fee,
            block.total_fee,
        )
        return False, "total fee mismatch"

    if getattr(block, "global_loaned", 0) != work_block.global_loaned:
        node.logger.debug(
            "Block verify global_loaned mismatch block=%s expected=%s actual=%s",
            _hex(block.expr_id),
            work_block.global_loaned,
            getattr(block, "global_loaned", 0),
        )
        return False, "global_loaned mismatch"
    if getattr(block, "global_defaulted", 0) != work_block.global_defaulted:
        node.logger.debug(
            "Block verify global_defaulted mismatch block=%s expected=%s actual=%s",
            _hex(block.expr_id),
            work_block.global_defaulted,
            getattr(block, "global_defaulted", 0),
        )
        return False, "global_defaulted mismatch"
    if getattr(block, "global_loan_count", 0) != work_block.global_loan_count:
        node.logger.debug(
            "Block verify global_loan_count mismatch block=%s expected=%s actual=%s",
            _hex(block.expr_id),
            work_block.global_loan_count,
            getattr(block, "global_loan_count", 0),
        )
        return False, "global_loan_count mismatch"

    applied_transactions = list(work_block.transactions or [])
    if len(applied_transactions) != len(tx_hashes):
        node.logger.debug(
            "Block verify tx count mismatch block=%s expected=%s actual=%s",
            _hex(block.expr_id),
            len(tx_hashes),
            len(applied_transactions),
        )
        return False, "tx count mismatch"

    expected_receipts: List[Receipt] = list(work_block.receipts or [])
    if len(expected_receipts) != len(applied_transactions):
        node.logger.debug(
            "Block verify receipt count mismatch block=%s expected=%s actual=%s",
            _hex(block.expr_id),
            len(applied_transactions),
            len(expected_receipts),
        )
        return False, "receipt count mismatch"

    expected_receipts_head = work_block.receipts_trie.root_hash if work_block.receipts_trie else None
    if (expected_receipts_head or ZERO32) != (block.receipts_hash or ZERO32):
        node.logger.debug(
            "Block verify receipts head mismatch block=%s expected=%s actual=%s",
            _hex(block.expr_id),
            _hex(expected_receipts_head or ZERO32),
            _hex(block.receipts_hash),
        )
        return False, "receipts head mismatch"

    stored_receipts_trie = RadixTree(root_hash=block.receipts_hash)
    for expected in expected_receipts:
        stored_expr = get_from_radix_tree(stored_receipts_trie, node, expected.transaction_hash)
        if stored_expr is None:
            node.logger.debug(
                "Block verify receipt not found in trie block=%s tx=%s",
                _hex(block.expr_id),
                _hex(expected.transaction_hash),
            )
            return False, f"receipt not found in trie tx={_hex(expected.transaction_hash)}"
        try:
            stored = Receipt.from_storage(node, stored_expr.hash())
        except Exception as exc:
            node.logger.debug(
                "Block verify failed loading receipt block=%s tx=%s error=%s",
                _hex(block.expr_id),
                _hex(expected.transaction_hash),
                exc,
            )
            return False, f"failed loading receipt tx={_hex(expected.transaction_hash)}"
        if stored.transaction_hash != expected.transaction_hash:
            node.logger.debug(
                "Block verify receipt tx mismatch block=%s",
                _hex(block.expr_id),
            )
            return False, f"receipt tx mismatch tx={_hex(expected.transaction_hash)}"
        if stored.status != expected.status:
            node.logger.debug(
                "Block verify receipt status mismatch block=%s",
                _hex(block.expr_id),
            )
            return False, f"receipt status mismatch tx={_hex(expected.transaction_hash)}"
        if stored.transaction_fee != expected.transaction_fee:
            node.logger.debug(
                "Block verify receipt transaction fee mismatch block=%s",
                _hex(block.expr_id),
            )
            return False, f"receipt transaction fee mismatch tx={_hex(expected.transaction_hash)}"
        if stored.storage_fee != expected.storage_fee:
            node.logger.debug(
                "Block verify receipt storage fee mismatch block=%s",
                _hex(block.expr_id),
            )
            return False, f"receipt storage fee mismatch tx={_hex(expected.transaction_hash)}"
        if stored.total_fee != expected.total_fee:
            node.logger.debug(
                "Block verify receipt total fee mismatch block=%s",
                _hex(block.expr_id),
            )
            return False, f"receipt total fee mismatch tx={_hex(expected.transaction_hash)}"
        if stored.logs_hash != expected.logs_hash:
            node.logger.debug(
                "Block verify receipt logs hash mismatch block=%s",
                _hex(block.expr_id),
            )
            return False, f"receipt logs hash mismatch tx={_hex(expected.transaction_hash)}"

    try:
        accounts_snapshot.update_trie(node)
    except Exception as exc:
        node.logger.debug(
            "Block verify failed updating accounts trie block=%s error=%s",
            _hex(block.expr_id),
            exc,
        )
        return False, "failed updating accounts trie"

    if accounts_snapshot.root_hash != block.accounts_hash:
        node.logger.debug(
            "Block verify accounts hash mismatch block=%s expected=%s actual=%s",
            _hex(block.expr_id),
            _hex(accounts_snapshot.root_hash),
            _hex(block.accounts_hash),
        )
        return False, "accounts hash mismatch"
    treasury_account = accounts_snapshot.get_account(TREASURY_ADDRESS, node)
    if treasury_account is None:
        node.logger.debug(
            "Block verify missing treasury account block=%s",
            _hex(block.expr_id),
        )
        return False, "missing treasury account"
    treasury_balance = treasury_account.balance or 0

    delta_fee = total_transaction_fee + total_storage_fee + (getattr(work_block, "total_mint", 0) or 0)
    delta_stake = treasury_balance
    expected_statistics = update_statistics(
        block.height,
        getattr(prev_block, "statistics", None),
        delta_fee,
        delta_stake,
    )
    if getattr(block, "statistics", None) != expected_statistics:
        node.logger.debug(
            "Block verify statistics mismatch block=%s",
            _hex(block.expr_id),
        )
        return False, "statistics mismatch"

    node.logger.debug("Block verify success block=%s", _hex(block.expr_id))
    return True, None
