from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.storage.radix.tree.get import exists_in_radix_tree

if TYPE_CHECKING:
    from astreum._node import Node
    from astreum.consensus.account.model import Account


_storage_account_cache: Optional[tuple[bytes, "Account"]] = None


def is_expr_in_latest_block(node: "Node", expr_id: bytes) -> bool:
    """Return True if *expr_id* is a key in the latest block's committed
    storage account data trie.

    Fail-closed: a node with no ``latest_block`` (or no storage account in it)
    yields ``False``, so unsynced nodes admit nothing.

    Args:
        node: A Node instance with a ``latest_block`` attribute (may be
            ``None`` while unsynced).
        expr_id: The 32-byte content hash to check for admission.

    Returns:
        True if the expr is committed in the latest block's storage
        account, False otherwise.
    """
    storage_account = _get_latest_storage_account(node)
    if storage_account is None:
        return False
    return exists_in_radix_tree(storage_account.data, node, expr_id)


def get_latest_storage_account(node: "Node") -> Optional["Account"]:
    """Return the latest block's STORAGE_ADDRESS account, or None.

    Public wrapper over the per-block-hash cached lookup in
    :func:`_get_latest_storage_account`.
    """
    return _get_latest_storage_account(node)


def _get_latest_storage_account(node: "Node") -> Optional["Account"]:
    """Fetch the STORAGE_ADDRESS account from ``node.latest_block``, cached per
    latest block hash so repeated admission checks cost one warm radix descent."""
    global _storage_account_cache

    latest_block = getattr(node, "latest_block", None)
    if latest_block is None:
        return None

    block_hash = getattr(latest_block, "expr_id", None)
    if (
        block_hash is not None
        and _storage_account_cache is not None
        and _storage_account_cache[0] == block_hash
    ):
        return _storage_account_cache[1]

    accounts = getattr(latest_block, "accounts", None)
    if accounts is None:
        return None

    account = accounts.get_account(STORAGE_ADDRESS, node)
    if account is not None and block_hash is not None:
        _storage_account_cache = (block_hash, account)
    return account
