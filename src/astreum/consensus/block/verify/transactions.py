from __future__ import annotations

from typing import Any, List, Optional

from astreum.expression import Expr, RESOLUTION_FULL, link_list_to_expr
from astreum.consensus.transaction.storage.model import STORAGE_RECORD_SHAPE, storage_record_to_expr
from astreum.storage.exprs import (
    get_expr_list,
    get_radix_all_from_network,
    prefetch_exprs_from_network,
)
from astreum.consensus.block.rate_window import update_statistics
from astreum.storage.radix import RadixTree, get_from_radix_tree, put_in_radix_tree
from astreum.consensus.models.accounts import Accounts
from astreum.consensus.account.model import ACCOUNT_SHAPE

ZERO32 = b"\x00" * 32
from astreum.consensus.models.receipt import Receipt
from astreum.consensus.block.create import create_block
from astreum.consensus.block.encoding.expr import get_block_expr
from astreum.consensus.transaction import apply_transaction_obj
from astreum.consensus.transaction.from_storage import get_transaction_from_storage
from astreum.consensus.transaction.storage.initial import (
    UnresolvedRecordExpr,
    generate_initial_storage_record,
)
from astreum.consensus.account import create_account
from astreum.consensus.constants import STORAGE_ADDRESS, TREASURY_ADDRESS
from astreum.consensus.validation.validator import current_validator
from astreum.crypto.bloom_search import ERA_SIZE
from astreum.crypto.bloom_tree import BloomTree
from astreum.expression.helpers import get_expr_builtin_tag


def _hex(value: Optional[bytes]) -> str:
    """Convert bytes to hex string for logging.

    Args:
        value: The bytes value.

    Returns:
        Hex string or str(value).
    """
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def _fetch_new_nodes(
    node: Any,
    root_hash: Optional[bytes],
    value_shape: bytes,
    root_block: Optional[bytes],
    contact: Any,
) -> None:
    """Fetch the nodes of a radix tree that are new in a block, with their values.

    The read stays in the block of *root_block*. It does not walk the older
    history of the tree. A node or a value that does not arrive is read later
    by the code that needs it, as before.

    Args:
        node: The node.
        root_hash: The root of the tree. Nothing is fetched for an empty tree.
        value_shape: The shape of the values of the tree.
        root_block: The block whose record holds the root.
        contact: The peer that holds the block.
    """
    if not root_hash or root_hash == ZERO32:
        return
    try:
        get_radix_all_from_network(
            node,
            root_hash,
            value_shape,
            contact=contact,
            block_hash=root_block,
            follow_refs=False,
        )
    except Exception as exc:
        node.logger.debug("Block verify batch fetch failed root=%s error=%s", _hex(root_hash), exc)


def verify_block_transactions(node: Any, block: Any) -> tuple[bool, Optional[str]]:
    """Verify receipts, transactions, and accounts hashes for this block.

    Args:
        node: The node.
        block: The block to verify.

    Returns:
        Tuple of (success, error_message).
    """
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
        """Load a list of hashes from storage.

        Args:
            head: The head hash.

        Returns:
            List of hashes or None.
        """
        if head == ZERO32:
            return []
        expr = get_expr_list(node, head, contact=block.contact, block_hash=block.expr_id)
        if expr is None:
            node.logger.debug(
                "Block verify missing list expr head=%s block=%s",
                _hex(head),
                _hex(block.expr_id),
            )
            return None
        result = []
        current = expr
        while current is not None and get_expr_builtin_tag(current) == "link":
            if current.head_hash is None:
                node.logger.debug(
                    "Block verify list node missing head_hash head=%s block=%s",
                    _hex(head),
                    _hex(block.expr_id),
                )
                return None
            result.append(current.head_hash)
            current = current.tail
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
        genesis_accounts = Accounts(root_hash=block.accounts_hash, block_hash=block.expr_id)
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

    # The parent's accounts trie is read next, and its record is built after
    # that. Fetch what is new in the parent in rounds of batched requests,
    # not one request per cell.
    _fetch_new_nodes(node, prev_block.accounts_hash, ACCOUNT_SHAPE, prev_block.expr_id, block.contact)

    # Start from the same accounts the creator builds on: the previous state
    # after the scheduled validator's stake has been halved. The creator draws
    # for the time the block carries, so this draws for the same time.
    try:
        scheduled_validator, accounts_snapshot = current_validator(
            node, prev_hash, target_time=block.timestamp, contact=block.contact
        )
    except Exception as exc:
        node.logger.debug(
            "Block verify failed deriving scheduled validator block=%s error=%s",
            _hex(block.expr_id),
            exc,
        )
        return False, f"failed deriving scheduled validator: {exc}"
    if scheduled_validator != block.validator_public_key_bytes:
        node.logger.debug(
            "Block verify unscheduled validator block=%s expected=%s actual=%s",
            _hex(block.expr_id),
            _hex(scheduled_validator),
            _hex(block.validator_public_key_bytes),
        )
        return False, "unscheduled validator"

    # The block the transactions are applied to is a real Block, as the
    # creator's is: the machine snapshots and restores it.
    work_block = create_block(
        chain_id=block.chain_id,
        previous_block_hash=block.previous_block_hash,
        previous_block=prev_block,
        height=block.height,
        timestamp=block.timestamp,
        accounts_hash=prev_block.accounts_hash,
        bloom_hash=prev_block.bloom_hash,
        total_transaction_fee=0,
        total_storage_fee=0,
        transactions_hash=None,
        receipts_hash=None,
        difficulty=block.difficulty,
        validator_public_key_bytes=block.validator_public_key_bytes,
        nonce=0,
        signature=None,
        accounts=accounts_snapshot,
        transactions=[],
        receipts=[],
        global_loaned=getattr(prev_block, "global_loaned", 0) or 0,
        global_defaulted=getattr(prev_block, "global_defaulted", 0) or 0,
        global_loan_count=getattr(prev_block, "global_loan_count", 0) or 0,
        contact=block.contact,
        expr_id=block.expr_id,
    )
    work_block.transactions = []
    work_block.receipts = []
    work_block.receipts_trie = None
    work_block.pending_exprs = []

    # The era's bloom tree, built as the creator builds it: the previous
    # block's tree (its nodes are in that block's record), or a new tree at
    # the first block of an era.
    if block.height % ERA_SIZE == 0:
        work_block.bloom_tree = BloomTree()
    else:
        work_block.bloom_tree = BloomTree(
            prev_block.bloom_hash,
            node,
            contact=block.contact,
            block_hash=prev_block.expr_id,
        )
        work_block.bloom_tree.set_leaf_start_hash(
            prev_block.height % ERA_SIZE, prev_block.expr_id, node
        )

    # Pre-commit previous block expr to storage data (replicating block builder)
    storage_account = work_block.accounts.get_account(STORAGE_ADDRESS, node)
    if storage_account is not None:
        _fetch_new_nodes(
            node,
            storage_account.data.root_hash,
            STORAGE_RECORD_SHAPE,
            storage_account.data.root_block,
            block.contact,
        )
        try:
            result = generate_initial_storage_record(
                node,
                work_block,
                get_block_expr(prev_block),
                follow_refs=True,
            )
        except UnresolvedRecordExpr as exc:
            node.logger.debug(
                "Block verify cannot build parent record block=%s error=%s",
                _hex(block.expr_id),
                exc,
            )
            return False, "failed building parent block record"
        if result is not None:
            record, _, _, _ = result
            put_in_radix_tree(storage_account.data, node, prev_block.expr_id, storage_record_to_expr(record))
            storage_account.data_hash = storage_account.data.root_hash

    # The tx hashes are all known now: fetch the missing tx trees in one batch
    # instead of one request per transaction.
    prefetch_exprs_from_network(
        node, tx_hashes, RESOLUTION_FULL, contact=block.contact, block_hash=block.expr_id
    )
    for tx_hash in tx_hashes:
        transaction = get_transaction_from_storage(
            node, tx_hash, contact=block.contact, block_hash=block.expr_id
        )
        if not transaction.verify_signature():
            node.logger.debug(
                "Block verify invalid transaction signature block=%s tx=%s",
                _hex(block.expr_id), _hex(tx_hash),
            )
            return False, f"invalid transaction signature tx={_hex(tx_hash)}"
        apply_transaction_obj(node, work_block, transaction)

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

    # The bloom tree the transactions built must be the one the header names.
    work_block.bloom_tree.rewrite_root()
    expected_bloom = (
        work_block.bloom_tree.root.expr().hash()
        if work_block.bloom_tree.root
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

    stored_receipts_trie = RadixTree(
        root_hash=block.receipts_hash, contact=block.contact, root_block=block.expr_id
    )
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
            stored = Receipt.from_storage(
                node, stored_expr.hash(), contact=block.contact, block_hash=block.expr_id
            )
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

    delta_fee = total_transaction_fee + total_storage_fee
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
