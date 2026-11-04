"""A validator and a separate storage provider. The provider claims a record.

The validator makes blocks, and each block gets a storage record. The provider
is a second node. It learns of a record through STORAGE_PUT, fetches the cells
from the validator, writes its records table, and sends the claim over UDP.
The validator puts the claim in a block and pays it.
"""

import unittest

from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.consensus.models.receipt import STATUS_SUCCESS
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.from_storage import get_transaction_from_storage
from astreum.consensus.transaction.storage.model import storage_record_from_storage
from astreum.storage.radix import get_from_radix_tree
from astreum.storage.records import iter_records_in_cold_storage

from tests.e2e.harness import (
    SLOT,
    STAKE,
    Network,
    account,
    block_at,
    chain,
    genesis_with,
    height,
    new_key,
    public,
    receipt,
    tx_hashes,
    wait_until,
)

PROVIDER_FUNDS = 10_000_000


class TestProviderClaimsFromValidator(unittest.TestCase):
    def test_provider_stores_a_record_and_the_validator_pays_the_claim(self) -> None:
        v_key, p_key = new_key(), new_key()
        provider_id = public(p_key)
        genesis = genesis_with(
            stakes={public(v_key): STAKE}, funds={provider_id: PROVIDER_FUNDS}
        )

        with Network() as net:
            validator = net.validator(v_key, genesis=genesis)
            # A plain peer cannot load the head block (see test_validator_and_peer),
            # so the provider is a validator-capable node with no stake.
            provider = net.user(
                validator,
                genesis,
                is_storage_provider=True,
                storage_secret_key=p_key,
                storage_claim_min_profit=0,
            )
            nodes = {"validator": validator, "provider": provider}

            held = wait_until(
                lambda: list(iter_records_in_cold_storage(provider)),
                timeout=SLOT * 20,
                message="the provider stores a record in its records table",
                nodes=nodes,
            )
            on_chain = {b.expr_id for b in chain(validator)}
            self.assertTrue(
                set(held) & on_chain, "a record of a block on the validator's chain"
            )

            def claim():
                for block in chain(validator):
                    for tx_hash in tx_hashes(validator, block):
                        tx = get_transaction_from_storage(validator, tx_hash)
                        if (
                            tx.code == TransactionCode.STORAGE_PAYMENT
                            and tx.sender == provider_id
                        ):
                            return block, tx
                return None

            block, tx = wait_until(
                claim,
                timeout=SLOT * 40,
                message="a STORAGE_PAYMENT from the provider in a block",
                nodes=nodes,
            )
            self.assertEqual(block.validator_public_key_bytes, public(v_key))
            self.assertEqual(receipt(validator, block, tx.hash).status, STATUS_SUCCESS)

            storage = account(validator, STORAGE_ADDRESS, block)
            winners = set()
            for record_id in held:
                head = get_from_radix_tree(storage.data, validator, record_id)
                if head is None:
                    continue
                record = storage_record_from_storage(validator, head.hash())
                winners.update(lane.winner for lane in record.lanes)
            self.assertIn(provider_id, winners, "the provider won a lane of a record it holds")
            self.assertGreater(account(validator, provider_id, block).balance, PROVIDER_FUNDS)

            wait_until(
                lambda: height(provider) >= block.height,
                timeout=SLOT * 10,
                message="the provider reaches the block with the claim",
                nodes=nodes,
            )
            self.assertEqual(
                block_at(provider, block.height).expr_id,
                block.expr_id,
                "the provider follows the same chain as the validator",
            )


if __name__ == "__main__":
    unittest.main()
