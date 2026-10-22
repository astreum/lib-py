"""A storage provider claims its reward when an era has passed.

Real history needs 1024 blocks (about 50 minutes) per era. The test starts
the chain at height 2040 instead. Genesis holds one storage record whose lane
was last paid at height 1000, so one full era has passed. The claim worker
sees that on its first pass and sends the claim.

Two stand-ins keep the test on the claim path (see test_send_transaction_api.py):
``send_transaction`` is replaced, and the validator and the provider are one
node. Genesis is built at height 2040, which ``verify_block_head`` rejects for
a genesis block, so no node here can verify the chain from its root.
"""

import unittest
from unittest import mock

from astreum.consensus.models.receipt import STATUS_SUCCESS
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.from_storage import get_transaction_from_storage
from astreum.consensus.transaction.storage.model import (
    StorageLane,
    StorageRecord,
    storage_record_cells,
    storage_record_from_storage,
    storage_record_to_expr,
)
from astreum.crypto.merkle import create_merkle_tree
from astreum.expression import ZERO32, bytes_, resolve_inner_exprs
from astreum.storage.cold import put_expr_in_cold_storage
from astreum.storage.exprs import put_expr_in_hot_storage
from astreum.storage.radix import get_from_radix_tree, put_in_radix_tree
from astreum.storage.records import put_record_in_cold_storage
from astreum.consensus.constants import STORAGE_ADDRESS

from tests.e2e.harness import (
    SLOT,
    STAKE,
    Network,
    account,
    chain,
    genesis_with,
    height,
    new_key,
    public,
    receipt,
    tx_hashes,
    wait_until,
)

START_HEIGHT = 2040
LANE_HEIGHT = 1000
RECORD_ID = b"\x5e" * 32
SLOTS = [bytes_(bytes([i]) * 24) for i in range(5)]
RECORD_SIZE = 1000
PROVIDER_FUNDS = 10_000_000


def _record(provider: bytes) -> StorageRecord:
    return StorageRecord(
        creation_block_hash=RECORD_ID,
        lanes=(
            StorageLane(block_hash=RECORD_ID, height=LANE_HEIGHT, winner=provider),
            StorageLane(block_hash=RECORD_ID, height=LANE_HEIGHT, winner=ZERO32),
        ),
        new_size=RECORD_SIZE,
        new_count=len(SLOTS),
        slots_root=create_merkle_tree([s.hash() for s in SLOTS]).root,
    )


def _send_to_own_queue(node, tx):
    """Stand-in for ``send_transaction`` on a node that is the only validator.

    ``send_transaction`` sends to validator peers and never to its own node
    (see test_send_transaction_api.py for its other fault).
    """
    tx.hash = tx.expr().hash()
    exprs, missed = resolve_inner_exprs(node, tx.expr())
    assert not missed, "claim transaction data unavailable"
    for expr in exprs:
        put_expr_in_hot_storage(node, expr)
    node._validation_transaction_queue.put(tx)
    return tx.hash


class TestStorageClaim(unittest.TestCase):
    def test_provider_claims_after_an_era(self) -> None:
        v_key, p_key = new_key(), new_key()
        provider = public(p_key)
        record = _record(provider)

        def customize(node, accounts, storage):
            put_in_radix_tree(storage.data, node, RECORD_ID, storage_record_to_expr(record))
            storage.data_hash = storage.data.root_hash
            accounts.pending_exprs.extend(storage_record_cells(record))
            for slot in SLOTS:
                put_expr_in_hot_storage(node, slot)

        genesis = genesis_with(
            stakes={public(v_key): STAKE},
            funds={provider: PROVIDER_FUNDS},
            start_height=START_HEIGHT,
            customize=customize,
        )

        with Network() as net, mock.patch(
            "astreum.consensus.transaction.send.send_transaction",
            _send_to_own_queue,
        ):
            # One node is the validator and the storage provider. The storage
            # key pays for the claim and wins the lane.
            node = net.validator(
                v_key,
                genesis=genesis,
                is_storage_provider=True,
                storage_secret_key=p_key,
            )
            for slot in SLOTS:
                put_expr_in_cold_storage(node, slot)
            put_record_in_cold_storage(node, RECORD_ID, [s.hash() for s in SLOTS])
            validator = node
            nodes = {"node": node}
            wait_until(
                lambda: height(node) >= START_HEIGHT + 1,
                timeout=SLOT * 10,
                message="the chain moves from the fake start height",
                nodes=nodes,
            )

            def claim():
                for block in chain(validator):
                    for tx_hash in tx_hashes(validator, block):
                        tx = get_transaction_from_storage(validator, tx_hash)
                        if tx.code == TransactionCode.STORAGE_PAYMENT and tx.sender == provider:
                            return block, tx
                return None

            block, tx = wait_until(
                claim,
                timeout=SLOT * 40,
                message="a STORAGE_PAYMENT from the provider in a block",
                nodes=nodes,
            )
            self.assertGreaterEqual(block.height, START_HEIGHT + 1)
            self.assertEqual(receipt(validator, block, tx.hash).status, STATUS_SUCCESS)

            storage = account(validator, STORAGE_ADDRESS, block)
            head = get_from_radix_tree(storage.data, validator, RECORD_ID)
            paid = storage_record_from_storage(validator, head.hash())
            self.assertEqual(paid.lanes[0].winner, provider)
            self.assertEqual(paid.lanes[0].height, block.height)
            self.assertEqual(paid.lanes[1], record.lanes[1], "the other lane is untouched")

            payout = RECORD_SIZE * (block.height - LANE_HEIGHT)
            gained = account(validator, provider, block).balance - PROVIDER_FUNDS
            self.assertGreater(gained, 0)
            self.assertLessEqual(gained, payout)


if __name__ == "__main__":
    unittest.main()
