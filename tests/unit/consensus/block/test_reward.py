import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.block.reward import fee_income, validator_reward  # noqa: E402


def _receipt(transaction_fee=0, data_fee=0, storage_fee=0):
    return SimpleNamespace(
        transaction_fee=transaction_fee, data_fee=data_fee, storage_fee=storage_fee
    )


class TestValidatorReward(unittest.TestCase):
    def test_an_empty_block_pays_one(self):
        self.assertEqual(validator_reward([]), 1)

    def test_the_reward_is_the_transaction_and_data_fees(self):
        receipts = [_receipt(3, 5, 100), _receipt(2, 0, 40)]
        self.assertEqual(validator_reward(receipts), 3 + 5 + 2)

    def test_the_storage_fee_is_not_paid_to_the_validator(self):
        self.assertEqual(validator_reward([_receipt(0, 0, 100)]), 1)


class TestFeeIncome(unittest.TestCase):
    def test_an_empty_block_adds_one(self):
        self.assertEqual(fee_income([]), 1)

    def test_fee_income_is_the_reward_plus_the_storage_fee(self):
        receipts = [_receipt(3, 5, 100), _receipt(2, 0, 40)]
        self.assertEqual(fee_income(receipts), (3 + 5 + 2) + (100 + 40))


if __name__ == "__main__":
    unittest.main()
