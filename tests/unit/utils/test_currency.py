import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.utils.currency import format_astre_amount, parse_astre_amount


class TestParseAmount(unittest.TestCase):
    def test_plain_integers(self):
        self.assertEqual(parse_astre_amount("0"), 0)
        self.assertEqual(parse_astre_amount("1000"), 1000)
        self.assertEqual(parse_astre_amount(" 42 "), 42)

    def test_negative(self):
        self.assertEqual(parse_astre_amount("-5"), -5)
        self.assertEqual(parse_astre_amount("-1.5k"), -1500)
        self.assertEqual(parse_astre_amount("+7"), 7)

    def test_unitless_number_is_base_units(self):
        self.assertEqual(parse_astre_amount("999"), 999)

    def test_scientific_notation_rejected(self):
        for text in ("1.0 e6", "1.0e6", "1E6", "1e+6", "1 e6", "1.5e3", "0.5e1"):
            with self.assertRaises(ValueError):
                parse_astre_amount(text)

    def test_digit_grouping(self):
        self.assertEqual(parse_astre_amount("1 000"), 1000)
        self.assertEqual(parse_astre_amount("1 000 000"), 1000000)
        self.assertEqual(parse_astre_amount("12 345 678"), 12345678)
        self.assertEqual(parse_astre_amount("-1 000 000"), -1000000)
        self.assertEqual(parse_astre_amount("1 000 kA"), 1000000)
        self.assertEqual(parse_astre_amount("1 000.5 kA"), 1000500)

    def test_invalid_grouping_rejected(self):
        for text in ("12 34", "1 0000", "1  000", "1 000  000"):
            with self.assertRaises(ValueError):
                parse_astre_amount(text)

    def test_base_unit_forms(self):
        self.assertEqual(parse_astre_amount("1 a"), 1)
        self.assertEqual(parse_astre_amount("1A"), 1)
        self.assertEqual(parse_astre_amount("1 astre"), 1)
        self.assertEqual(parse_astre_amount("1 Astre"), 1)
        self.assertEqual(parse_astre_amount("1ASTRE"), 1)

    def test_kilo(self):
        self.assertEqual(parse_astre_amount("1k"), 1000)
        self.assertEqual(parse_astre_amount("1 k"), 1000)
        self.assertEqual(parse_astre_amount("1kA"), 1000)
        self.assertEqual(parse_astre_amount("1 kastre"), 1000)
        self.assertEqual(parse_astre_amount("1 kiloastre"), 1000)
        self.assertEqual(parse_astre_amount("1 KiloAstre"), 1000)
        self.assertEqual(parse_astre_amount("1.5k"), 1500)

    def test_mega(self):
        self.assertEqual(parse_astre_amount("1MA"), 1000000)
        self.assertEqual(parse_astre_amount("1 ma"), 1000000)
        self.assertEqual(parse_astre_amount("1 M"), 1000000)
        self.assertEqual(parse_astre_amount("1 mAstre"), 1000000)
        self.assertEqual(parse_astre_amount("1 MAstre"), 1000000)
        self.assertEqual(parse_astre_amount("1 megaastre"), 1000000)
        self.assertEqual(parse_astre_amount("1 MegaAstre"), 1000000)
        self.assertEqual(parse_astre_amount("1.5 MA"), 1500000)

    def test_giga_and_tera(self):
        self.assertEqual(parse_astre_amount("1G"), 10**9)
        self.assertEqual(parse_astre_amount("1 gigaastre"), 10**9)
        self.assertEqual(parse_astre_amount("1 GAstre"), 10**9)
        self.assertEqual(parse_astre_amount("1T"), 10**12)
        self.assertEqual(parse_astre_amount("1 teraAstre"), 10**12)
        self.assertEqual(parse_astre_amount("2 t"), 2 * 10**12)

    def test_peta_through_quetta(self):
        self.assertEqual(parse_astre_amount("1P"), 10**15)
        self.assertEqual(parse_astre_amount("1 p"), 10**15)
        self.assertEqual(parse_astre_amount("1 Pa"), 10**15)
        self.assertEqual(parse_astre_amount("1 pastre"), 10**15)
        self.assertEqual(parse_astre_amount("1 PetaAstre"), 10**15)
        self.assertEqual(parse_astre_amount("1.5 Ea"), 10**18 + 5 * 10**17)
        self.assertEqual(parse_astre_amount("1Z"), 10**21)
        self.assertEqual(parse_astre_amount("1 zettaastre"), 10**21)
        self.assertEqual(parse_astre_amount("2.5 Y"), 25 * 10**23)
        self.assertEqual(parse_astre_amount("1 r"), 10**27)
        self.assertEqual(parse_astre_amount("1 RonnaAstre"), 10**27)
        self.assertEqual(parse_astre_amount("1Q"), 10**30)
        self.assertEqual(parse_astre_amount("1 QuettaAstre"), 10**30)

    def test_exa_unambiguous(self):
        self.assertEqual(parse_astre_amount("1 E"), 10**18)
        self.assertEqual(parse_astre_amount("1E"), 10**18)
        self.assertEqual(parse_astre_amount("1EA"), 10**18)
        self.assertEqual(parse_astre_amount("1 ea"), 10**18)
        self.assertEqual(parse_astre_amount("1.5 Ea"), 15 * 10**17)
        self.assertEqual(parse_astre_amount("1 exaastre"), 10**18)

    def test_internal_whitespace(self):
        self.assertEqual(parse_astre_amount("  1   MAstre  "), 1000000)

    def test_fractional_result_raises(self):
        with self.assertRaises(ValueError):
            parse_astre_amount("1.5")
        with self.assertRaises(ValueError):
            parse_astre_amount("1e-1")
        with self.assertRaises(ValueError):
            parse_astre_amount("0.5 astre")

    def test_invalid_text_raises(self):
        for text in ("", "abc", "1 x", "1 kastre+", "$5", "1..2", "1 astre astre"):
            with self.assertRaises(ValueError):
                parse_astre_amount(text)

    def test_non_string_raises(self):
        with self.assertRaises(ValueError):
            parse_astre_amount(1000)  # type: ignore[arg-type]


class TestFormatAmount(unittest.TestCase):
    def test_below_1000_short(self):
        self.assertEqual(format_astre_amount(0), "0 A")
        self.assertEqual(format_astre_amount(999), "999 A")

    def test_below_1000_full(self):
        self.assertEqual(format_astre_amount(999, style="full"), "999 Astre")

    def test_short_prefixes(self):
        self.assertEqual(format_astre_amount(1000), "1 kA")
        self.assertEqual(format_astre_amount(1500), "1.5 kA")
        self.assertEqual(format_astre_amount(1000000), "1 MA")
        self.assertEqual(format_astre_amount(2500000), "2.5 MA")
        self.assertEqual(format_astre_amount(10**9), "1 GA")
        self.assertEqual(format_astre_amount(10**12), "1 TA")
        self.assertEqual(format_astre_amount(10**15), "1 PA")
        self.assertEqual(format_astre_amount(10**18), "1 EA")
        self.assertEqual(format_astre_amount(10**21), "1 ZA")
        self.assertEqual(format_astre_amount(10**24), "1 YA")
        self.assertEqual(format_astre_amount(10**27), "1 RA")
        self.assertEqual(format_astre_amount(10**30), "1 QA")

    def test_tera_boundary_stays_tera(self):
        self.assertEqual(format_astre_amount(10**15 - 1), "999.999999999999 TA")
        self.assertEqual(format_astre_amount(10**12 - 1), "999.999999999 GA")

    def test_full_names(self):
        self.assertEqual(format_astre_amount(1000, style="full"), "1 kiloAstre")
        self.assertEqual(format_astre_amount(1000000, style="full"), "1 MegaAstre")
        self.assertEqual(format_astre_amount(1500000, style="full"), "1.5 MegaAstre")
        self.assertEqual(format_astre_amount(10**9, style="full"), "1 GigaAstre")
        self.assertEqual(format_astre_amount(10**12, style="full"), "1 TeraAstre")
        self.assertEqual(format_astre_amount(10**15, style="full"), "1 PetaAstre")
        self.assertEqual(format_astre_amount(10**18, style="full"), "1 ExaAstre")
        self.assertEqual(format_astre_amount(10**21, style="full"), "1 ZettaAstre")
        self.assertEqual(format_astre_amount(10**24, style="full"), "1 YottaAstre")
        self.assertEqual(format_astre_amount(10**27, style="full"), "1 RonnaAstre")
        self.assertEqual(format_astre_amount(10**30, style="full"), "1 QuettaAstre")

    def test_negative(self):
        self.assertEqual(format_astre_amount(-1500), "-1.5 kA")

    def test_trailing_zeros_trimmed(self):
        self.assertEqual(format_astre_amount(1500000), "1.5 MA")
        self.assertEqual(format_astre_amount(1200000), "1.2 MA")

    def test_invalid_style_raises(self):
        with self.assertRaises(ValueError):
            format_astre_amount(1000, style="long")

    def test_non_int_raises(self):
        with self.assertRaises(ValueError):
            format_astre_amount("1000")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            format_astre_amount(True)  # type: ignore[arg-type]


class TestRoundTrip(unittest.TestCase):
    def test_round_trip_exact_multiples(self):
        for amount in (
            0,
            1,
            999,
            1000,
            1500,
            10**6,
            2711000000,
            10**12,
            7 * 10**12,
            10**15,
            10**18,
            25 * 10**17,
            10**21,
            10**24,
            10**27,
            10**30,
        ):
            self.assertEqual(parse_astre_amount(format_astre_amount(amount)), amount)
            self.assertEqual(
                parse_astre_amount(format_astre_amount(amount, style="full")), amount
            )

    def test_round_trip_non_multiples(self):
        for amount in (
            1001,
            1234567,
            999999999,
            1234567890123,
            10**15 - 1,
            12345678901234567,
            10**18 - 1,
            999999999999999999999999,
        ):
            self.assertEqual(parse_astre_amount(format_astre_amount(amount)), amount)
            self.assertEqual(
                parse_astre_amount(format_astre_amount(amount, style="full")), amount
            )


if __name__ == "__main__":
    unittest.main()
