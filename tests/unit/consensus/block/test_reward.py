import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.models.statistics import BlockStatistics  # noqa: E402
from astreum.consensus.block.reward import fee_income, validator_reward  # noqa: E402


def _receipt(base_fee=0, execution_fee=0, data_fee=0, storage_fee=0):
    return SimpleNamespace(
        base_fee=base_fee,
        execution_fee=execution_fee,
        data_fee=data_fee,
        storage_fee=storage_fee,
        total_fee=base_fee + execution_fee + data_fee + storage_fee,
    )


class TestValidatorReward(unittest.TestCase):
    def test_an_empty_block_pays_nothing(self):
        self.assertEqual(validator_reward([]), 0)

    def test_the_reward_is_the_transaction_and_data_fees(self):
        receipts = [
            _receipt(base_fee=3, data_fee=5, storage_fee=100),
            _receipt(base_fee=2, storage_fee=40),
        ]
        self.assertEqual(validator_reward(receipts), 3 + 5 + 2)

    def test_the_storage_fee_is_not_paid_to_the_validator(self):
        self.assertEqual(validator_reward([_receipt(storage_fee=100)]), 0)

    def test_only_the_named_fee_fields_are_paid(self):
        # A receipt whose total_fee carries a fee outside the three rewarded
        # fields must not pass that fee to the validator.
        receipt = _receipt(base_fee=3, data_fee=5, storage_fee=100)
        receipt.total_fee += 7
        self.assertEqual(validator_reward([receipt]), 3 + 5)


class TestFeeIncome(unittest.TestCase):
    def test_an_empty_block_adds_nothing(self):
        self.assertEqual(fee_income([]), 0)

    def test_an_empty_block_leaves_every_statistics_delta_at_zero(self):
        previous = BlockStatistics(7, 100, 200, 3, 11, 500)
        statistics = previous.updated(
            total_balance=100, fee_income=fee_income([]), claims=0,
            tx_fees=0, tx_bytes=0,
        )
        # Only the cumulative balance advances: it adds the supply level of
        # this block. The fee, claim, tx-fee, and tx-byte counters stand still.
        self.assertEqual(statistics, BlockStatistics(7, 100, 300, 3, 11, 500))

    def test_fee_income_is_the_reward_plus_the_storage_fee(self):
        receipts = [
            _receipt(base_fee=3, data_fee=5, storage_fee=100),
            _receipt(base_fee=2, storage_fee=40),
        ]
        self.assertEqual(fee_income(receipts), (3 + 5 + 2) + (100 + 40))


if __name__ == "__main__":
    unittest.main()
