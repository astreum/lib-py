from __future__ import annotations

from typing import Any

from blake3 import blake3

from astreum.expression import Expr, get_expr_tag, get_expr_value, resolve_list_exprs, int_, bytes_, link
from astreum.expression.expr import _encode_int
from astreum.expression.encoding import decode_expr_from_bytes
from astreum.expression import ZERO32, NIL
from astreum.storage.radix import get_from_radix_tree_with_block, put_in_radix_tree
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.storage.initial import block_scope
from astreum.consensus.transaction.storage.model import StorageRecord
from astreum.crypto.bloom_search import ERA_SIZE
from astreum.crypto.merkle import MerkleTree, verify_merkle_tree_proof

# Fib(13) = 233, largest fib <= 256-bit hash size
_N = 13

# A proof has one sibling per tree level; 64 levels cover 2**64 slots.
MAX_PROOF_LEN = 64


def _fib(n: int) -> int:
    """Return the nth Fibonacci number (fib(0)=0, fib(1)=1, fib(2)=1, ...)."""

    Args:
        n (int): Description.

    if n < 0:
        return 0
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def _leading_zero_bits(buf: bytes) -> int:

    Args:
        buf (bytes): Description.

    zeros = 0
    for byte in buf:
        if byte == 0:
            zeros += 8
            continue
        zeros += 8 - byte.bit_length()
        break
    return zeros


def _required_bits(*, sender: bytes, last_payment_winner: bytes,
                   block_height: int, last_payment_height: int,
                   base_bits: int) -> int:
    if sender == last_payment_winner:
        return base_bits
    eras_elapsed = (block_height - last_payment_height) // ERA_SIZE
    penalty = _fib(_N - min(eras_elapsed, _N))
    return base_bits + penalty


Claim = tuple[bytes, bytes, list[bytes], int]


def _is_nil(e: Expr | None) -> bool:

    Args:
        e (Expr | None): Description.

    return e is None or e is NIL or (
        e._tag == "link" and e._head is None and e._tail is None
        and e._head_hash in (None, ZERO32) and e._tail_hash in (None, ZERO32)
    )


def _list_items(e: Expr | None, limit: int) -> list[Expr] | None:
    """Items of a loaded link list, or None if it is not one (or is longer than *limit*)."""

    Args:
        e (Expr | None): Description.
        limit (int): Description.

    items: list[Expr] = []
    while not _is_nil(e):
        if e._tag != "link" or e._head is None:
            return None
        items.append(e._head)
        if len(items) > limit:
            return None
        e = e._tail
    return items


def _parse_claim(
    claim_expr: Expr, node: Any = None, contact=None, block_hash=None
) -> Claim | None:
    """Parse a single claim from its Expr representation.

    Claim format::

        Link(Bytes(storage_id),
          Link(Bytes(slot_expr_bytes),
            Link(<proof: list of Bytes(sibling hash), leaf to root>,
              Link(Int(nonce), NIL))))

    The proof carries no index: the challenge fixes it. Returns
    ``(storage_id, slot_expr_bytes, proof, nonce)``.
    """
    items = _list_items(claim_expr, 4)
    if items is None or len(items) != 4:
        return None
    storage_id_e, slot_e, proof_e, nonce_e = items
    if storage_id_e._tag != "bytes" or slot_e._tag != "bytes":
        return None
    try:
        # A fetched int holds its payload by hash.
        if get_expr_tag(nonce_e, node, contact, block_hash) != "int":
            return None
        nonce = get_expr_value(nonce_e, node, contact, block_hash)
    except ValueError:
        return None
    proof_items = _list_items(proof_e, MAX_PROOF_LEN)
    if proof_items is None or any(i._tag != "bytes" for i in proof_items):
        return None
    proof = [i.value for i in proof_items]
    return storage_id_e.value, slot_e.value, proof, nonce


def challenge_index(last_payment_block_hash: bytes, storage_id: bytes, new_count: int) -> int:
    """Index of the slot a record is challenged on."""

    Args:
        last_payment_block_hash (bytes): Description.
        storage_id (bytes): Description.
        new_count (int): Description.

    seed = blake3(last_payment_block_hash + storage_id).digest()
    return int.from_bytes(seed[:8], "little", signed=False) % new_count


def _check_claim(
    node: Any,
    block: object,
    transaction: Transaction,
    storage_account: Any,
    claim: Claim,
) -> tuple[StorageRecord, int] | None:
    """Verify one claim against its record. Returns ``(record, payout)`` or None.

    Fetches nothing but the record: the claim carries the challenged slot and
    its proof, and the record carries the root they are checked against.
    """
    storage_id, slot_bytes, proof, nonce = claim

    # 1. The record, from the storage trie
    record_head, record_block = get_from_radix_tree_with_block(
        storage_account.data, node, storage_id
    )
    if not record_head or record_head == ZERO32:
        return None
    record = StorageRecord.from_storage(
        node, record_head.hash(), contact=block.contact, block_hash=record_block
    )
    if record is None:
        return None

    last_payment_block_hash = record.last_payment_block_hash
    if len(last_payment_block_hash) != 32:
        return None
    if record.new_count <= 0 or len(record.slots_root) != 32:
        return None

    # 2. The challenged slot is in the record: its proof reaches slots_root at
    #    the index the challenge fixes
    index = challenge_index(last_payment_block_hash, storage_id, record.new_count)
    try:
        slot_id = decode_expr_from_bytes(slot_bytes).hash()
    except Exception:
        return None
    tree = MerkleTree(root=record.slots_root, count=record.new_count)
    if not verify_merkle_tree_proof(tree, index, slot_id, proof):
        return None

    # 3. Proof of work over the slot's bytes
    sender_bytes = transaction.sender
    work_hash = blake3(
        last_payment_block_hash
        + sender_bytes
        + storage_id
        + slot_id
        + slot_bytes
        + _encode_int(nonce)
    ).digest()
    required_bits = _required_bits(
        sender=sender_bytes,
        last_payment_winner=record.last_payment_winner,
        block_height=block.height,
        last_payment_height=record.last_payment_height,
        base_bits=0,  # storage payment uses pure PoW; no XOR-distance base
    )
    if _leading_zero_bits(work_hash) < required_bits:
        return None

    # 4. Payout
    if record.new_size <= 0 or block.height <= record.last_payment_height:
        return None
    payout = record.new_size * (block.height - record.last_payment_height)
    if payout <= 0:
        return None
    return record, payout


def handle_storage_payment_contract(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
    sender_account: Any,
    storage_account: Any,
    payload: Expr,
) -> bool:
    """Handle a storage-payment contract transaction.

    ``payload`` is the transaction's ``data`` field, which is an Expr
    representing a link list of claims.

    Multi-claim: each claim is verified independently.  Invalid claims are
    silently skipped.  The transaction succeeds if at least one claim is valid.
    A valid claim pays the sender and rewrites its record at once, so a second
    claim on the same record in the same transaction no longer pays.

    Returns True if at least one claim was valid.
    """
    try:
        # Resolve the link list of claims
        claims_nodes, missed = resolve_list_exprs(
            node, payload, contact=block.contact, block_hash=block_scope(block)
        )
        if missed:
            return False

        parsed_claims: list[Claim] = []
        for claim_node in claims_nodes:
            parsed = _parse_claim(claim_node, node, block.contact, block_scope(block))
            if parsed is not None:
                parsed_claims.append(parsed)

        if not parsed_claims:
            return False

        any_valid = False
        for claim in parsed_claims:
            checked = _check_claim(node, block, transaction, storage_account, claim)
            if checked is None:
                continue
            record, payout = checked
            storage_id = claim[0]

            sender_account.balance += payout

            updated = StorageRecord(
                creation_block_hash=record.creation_block_hash,
                last_payment_block_hash=block.previous_block_hash,
                last_payment_height=block.height,
                last_payment_winner=transaction.sender,
                new_size=record.new_size,
                new_count=record.new_count,
                slots_root=record.slots_root,
            )
            put_in_radix_tree(storage_account.data, node, storage_id, updated.expr())
            storage_account.data_hash = storage_account.data.root_hash
            block.pending_exprs.extend(updated.cells())
            any_valid = True

        return any_valid
    except Exception:
        return False
