from typing import List, Optional, TYPE_CHECKING

from astreum.expression import Expr, ZERO32
from astreum.consensus.models.accounts import Accounts
from astreum.consensus.models.block import Block

if TYPE_CHECKING:
    from astreum.storage.radix import RadixTree
    from astreum.consensus.transaction.model import Transaction
    from astreum.consensus.transaction.storage.pending import PendingStorageContract


def create_block(
    *,
    chain_id: int,
    previous_block_hash: bytes,
    previous_block: Optional[Block],
    height: int,
    timestamp: Optional[int],
    accounts_hash: Optional[bytes],
    total_transaction_fee: Optional[int],
    total_storage_fee: Optional[int],
    transactions_hash: Optional[bytes],
    receipts_hash: Optional[bytes],
    difficulty: Optional[int],
    validator_public_key_bytes: Optional[bytes],
    nonce: Optional[int] = None,
    bloom_hash: Optional[bytes] = None,
    previous_era_hash: Optional[bytes] = None,
    signature: Optional[bytes] = None,
    total_mint: int = 0,
    expr_id: Optional[bytes] = None,
    body_hash: Optional[bytes] = None,
    accounts: Optional["RadixTree"] = None,
    transactions: Optional[List["Transaction"]] = None,
    receipts: Optional[List["Receipt"]] = None,
    receipts_trie: Optional["RadixTree"] = None,
    statistics: Optional[list] = None,
    pending_exprs: Optional[List[Expr]] = None,
    pending_storage_contracts: Optional[List["PendingStorageContract"]] = None,
    global_loaned: int = 0,
    global_defaulted: int = 0,
    global_loan_count: int = 0,
) -> Block:
    previous_block_hash = previous_block_hash or ZERO32
    accounts_hash = accounts_hash or ZERO32
    transactions_hash = transactions_hash or ZERO32
    receipts_hash = receipts_hash or ZERO32
    bloom_hash = bloom_hash or ZERO32
    previous_era_hash = previous_era_hash or ZERO32
    body_hash = body_hash or ZERO32
    expr_id = expr_id or ZERO32
    signature = signature or ZERO32
    validator_public_key_bytes = validator_public_key_bytes or ZERO32
    nonce = nonce or 0

    if accounts is None and accounts_hash and accounts_hash != ZERO32:
        accounts = Accounts(root_hash=accounts_hash)
    return Block(
        chain_id=chain_id,
        previous_block_hash=previous_block_hash,
        previous_block=previous_block,
        height=height,
        timestamp=timestamp,
        accounts_hash=accounts_hash,
        total_transaction_fee=total_transaction_fee,
        total_storage_fee=total_storage_fee,
        transactions_hash=transactions_hash,
        receipts_hash=receipts_hash,
        difficulty=difficulty,
        validator_public_key_bytes=validator_public_key_bytes,
        nonce=nonce,
        bloom_hash=bloom_hash,
        previous_era_hash=previous_era_hash,
        signature=signature,
        total_mint=total_mint,
        expr_id=expr_id,
        body_hash=body_hash,
        accounts=accounts,
        transactions=transactions,
        receipts=receipts,
        receipts_trie=receipts_trie,
        statistics=statistics,
        pending_exprs=pending_exprs,
        pending_storage_contracts=pending_storage_contracts,
        global_loaned=global_loaned,
        global_defaulted=global_defaulted,
        global_loan_count=global_loan_count,
    )
