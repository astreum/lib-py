"""The storage fee is priced by the measured byte pair and the quote."""

import sys
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.block.rate import (
    calculate_fee_rate,
    calculate_quote_rate,
    calculate_storage_fee,
    next_storage_price,
)
from astreum.consensus.constants import PROVIDERS_PER_RECORD
from astreum.consensus.models.statistics import BlockStatistics


def _previous_block(
    *,
    total_fees=3,
    cumulative_total_balance=6,
    total_tx_fees=2,
    total_tx_bytes=8,
):
    return SimpleNamespace(
        height=0,
        statistics=BlockStatistics(
            total_fees,
            1,
            cumulative_total_balance,
            0,
            total_tx_fees,
            total_tx_bytes,
        ),
    )


def _block(*, height=1, **previous):
    return SimpleNamespace(
        height=height,
        previous_block=_previous_block(**previous),
    )


class TestFeeRate(unittest.TestCase):
    def test_fee_rate_is_the_demand_pair_ratio(self):
        block = _block(total_tx_fees=3, total_tx_bytes=7)
        self.assertEqual(calculate_fee_rate(block), Fraction(3, 7))

    def test_model_block_fee_rate_is_total_tx_fees_over_total_tx_bytes(self):
        # Model intensity 0.5: the pair carries one half fee per byte.
        block = _block(total_tx_fees=1, total_tx_bytes=2)
        self.assertEqual(
            calculate_fee_rate(block),
            Fraction(1, 2),
        )

    def test_raises_when_the_previous_block_has_no_statistics(self):
        block = SimpleNamespace(
            previous_block=SimpleNamespace(statistics=None)
        )
        with self.assertRaisesRegex(ValueError, "statistics are required"):
            calculate_fee_rate(block)

    def test_raises_when_pair_has_no_bytes(self):
        block = _block(total_tx_fees=3, total_tx_bytes=0)
        with self.assertRaisesRegex(ValueError, "total_tx_bytes"):
            calculate_fee_rate(block)

    def test_raises_when_pair_has_no_fees(self):
        block = _block(total_tx_fees=0, total_tx_bytes=7)
        with self.assertRaisesRegex(ValueError, "total_tx_fees"):
            calculate_fee_rate(block)

    def test_raises_without_previous_block(self):
        with self.assertRaises(ValueError):
            calculate_fee_rate(SimpleNamespace(previous_block=None))


class TestNextStoragePrice(unittest.TestCase):
    def test_reads_the_head_as_previous_and_the_next_height(self):
        head = _previous_block(total_tx_fees=3, total_tx_bytes=6)
        head.height = 5
        fee_rate, quote = next_storage_price(head)

        self.assertEqual(fee_rate, Fraction(1, 2))
        expected_block = SimpleNamespace(height=6, previous_block=head)
        self.assertEqual(quote, calculate_quote_rate(expected_block))


class TestStorageFee(unittest.TestCase):
    def test_fee_is_two_ceil_of_bytes_times_fee_rate_over_quote(self):
        self.assertEqual(PROVIDERS_PER_RECORD, 2)
        block = _block(
            total_fees=3,
            cumulative_total_balance=6,
            total_tx_fees=2,
            total_tx_bytes=8,
        )
        fee_rate = Fraction(2, 8)
        quote = calculate_quote_rate(block)
        for total_bytes in (0, 1, 100, 5000):
            numerator = total_bytes * fee_rate.numerator * quote.denominator
            denominator = fee_rate.denominator * quote.numerator
            single = -(-numerator // denominator)  # ceil
            self.assertEqual(
                calculate_storage_fee(block, total_bytes),
                2 * single,
                total_bytes,
            )

    def test_fee_uses_the_measured_pair_and_quote(self):
        # r_bar = 3 / 6 = 1 / 2 and h = 1, so r_q is a little above 1 / 6.
        # fee_rate = 2 / 8 = 1 / 4, so 101 bytes price a little below
        # 151.5 and the ceiling is 152.
        block = _block(
            total_fees=3,
            cumulative_total_balance=6,
            total_tx_fees=2,
            total_tx_bytes=8,
        )
        self.assertEqual(calculate_storage_fee(block, 101), 2 * 152)

    def test_fee_raises_without_previous_block(self):
        with self.assertRaisesRegex(ValueError, "previous block"):
            calculate_storage_fee(SimpleNamespace(previous_block=None), 1000)

    def test_fee_raises_on_zero_total_fee(self):
        block = _block(
            total_fees=0,
            cumulative_total_balance=6,
            total_tx_fees=2,
            total_tx_bytes=8,
        )
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            calculate_storage_fee(block, 100)

    def test_fee_raises_on_empty_demand_pair(self):
        block = _block(total_tx_fees=0, total_tx_bytes=0)
        with self.assertRaises(ValueError):
            calculate_storage_fee(block, 100)


if __name__ == "__main__":
    unittest.main()
