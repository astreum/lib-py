
from typing import Any, List, Optional, TYPE_CHECKING

from astreum.expression import Expr, ZERO32
from astreum.consensus.models.accounts import Accounts

if TYPE_CHECKING:
    from astreum.storage.radix import RadixTree
    from astreum.consensus.transaction.model import Transaction
    from astreum.consensus.transaction.storage.pending import PendingStorageContract
    from astreum.crypto.bloom_tree import BloomTree


class Block:
    expr_id: Optional[bytes]
    chain_id: int
    previous_block_hash: bytes
    previous_block: Optional["Block"]

    height: int
    timestamp: Optional[int]
    accounts_hash: Optional[bytes]
    total_transaction_fee: Optional[int]
    total_storage_fee: Optional[int]
    transactions_hash: Optional[bytes]
    receipts_hash: Optional[bytes]
    difficulty: Optional[int]
    validator_public_key_bytes: Optional[bytes]
    nonce: Optional[int]
    bloom_hash: Optional[bytes]
    previous_era_hash: Optional[bytes]

    body_hash: Optional[bytes]
    signature: Optional[bytes]
    total_mint: int

    accounts: Optional["RadixTree"]
    transactions: Optional[List["Transaction"]]
    receipts: Optional[List["Receipt"]]
    receipts_trie: Optional["RadixTree"]
    statistics: Optional[list]
    pending_exprs: List[Expr]
    pending_storage_contracts: List["PendingStorageContract"]
    bloom_tree: Optional["BloomTree"]
    pending_bloom_keys: set[bytes]
    global_loaned: int
    global_defaulted: int
    global_loan_count: int
    _expr: Optional["Expr"]
    
    def __init__(
        self,
        *,
        chain_id: int,
        previous_block_hash: bytes,
        previous_block: Optional["Block"],
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
    ) -> None:
        self.expr_id = expr_id
        self.chain_id = chain_id
        self.previous_block_hash = previous_block_hash
        self.previous_block = previous_block
        self.height = height
        self.timestamp = timestamp
        self.accounts_hash = accounts_hash
        self.total_transaction_fee = total_transaction_fee
        self.total_storage_fee = total_storage_fee
        self.transactions_hash = transactions_hash
        self.receipts_hash = receipts_hash
        self.difficulty = difficulty
        self.validator_public_key_bytes = validator_public_key_bytes
        self.nonce = nonce
        self.bloom_hash = bloom_hash
        self.previous_era_hash = previous_era_hash
        self.body_hash = body_hash
        self.signature = signature
        self.total_mint = total_mint
        if accounts is None and accounts_hash and accounts_hash != ZERO32:
            self.accounts = Accounts(root_hash=accounts_hash)
        else:
            self.accounts = accounts
        self.transactions = transactions
        self.receipts = receipts
        self.receipts_trie = receipts_trie
        self.pending_exprs = list(pending_exprs or [])
        self.statistics = statistics or []
        self.pending_storage_contracts = list(pending_storage_contracts or [])
        self.bloom_tree = None
        self.pending_bloom_keys = set()
        self.global_loaned = global_loaned
        self.global_defaulted = global_defaulted
        self.global_loan_count = global_loan_count
        self._expr = None

    def snapshot(self) -> tuple:
        saved_cache = {
            addr: acct.clone()
            for addr, acct in self.accounts._cache.items()
        }
        saved_pending_exprs = list(self.pending_exprs)
        saved_pending_storage = list(self.pending_storage_contracts)
        saved_total_mint = self.total_mint
        saved_global_loaned = self.global_loaned
        saved_global_defaulted = self.global_defaulted
        saved_global_loan_count = self.global_loan_count
        return (
            saved_cache,
            saved_pending_exprs,
            saved_pending_storage,
            saved_total_mint,
            saved_global_loaned,
            saved_global_defaulted,
            saved_global_loan_count,
        )

    def restore(self, snapshot: tuple) -> None:
        (
            saved_cache,
            saved_pending_exprs,
            saved_pending_storage,
            saved_total_mint,
            saved_global_loaned,
            saved_global_defaulted,
            saved_global_loan_count,
        ) = snapshot
        self.accounts._cache = saved_cache
        self.pending_exprs = saved_pending_exprs
        self.pending_storage_contracts = saved_pending_storage
        self.total_mint = saved_total_mint
        self.global_loaned = saved_global_loaned
        self.global_defaulted = saved_global_defaulted
        self.global_loan_count = saved_global_loan_count

    @property
    def total_fee(self) -> int:
        return (self.total_transaction_fee or 0) + (self.total_storage_fee or 0)

    @property
    def cumulative_total_fee(self) -> int:
        if self.statistics:
            return self.statistics[0][0]
        return 0

    @property
    def cumulative_stake(self) -> int:
        if self.statistics:
            return self.statistics[0][1]
        return 0


