"""Тесты денежного ввода и форматирования для списка покупок."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import money  # noqa: E402


class MoneyParsingTests(unittest.TestCase):
    def test_accepts_russian_and_dot_decimals(self):
        self.assertEqual(money.parse_price_to_cents("2,50"), 250)
        self.assertEqual(money.parse_price_to_cents("2.5"), 250)
        self.assertEqual(money.parse_price_to_cents("2,50 руб."), 250)
        self.assertEqual(money.parse_price_to_cents("2,50 BYN"), 250)
        self.assertEqual(money.parse_price_to_cents("1 234,50"), 123450)
        self.assertEqual(money.parse_price_to_cents("₽12"), 1200)

    def test_zero_resets_price(self):
        self.assertEqual(money.parse_price_to_cents("0"), 0)

    def test_rejects_garbage_and_negatives(self):
        for value in ("", "abc", "-5", "2,555", "1.2.3", None):
            with self.subTest(value=value):
                self.assertIsNone(money.parse_price_to_cents(value))

    def test_format_money(self):
        self.assertEqual(money.format_money(250), "2,50 BYN")
        self.assertEqual(money.format_money(0), "0,00 BYN")
        self.assertEqual(money.format_money(123450), "1 234,50 BYN")


if __name__ == "__main__":
    unittest.main()
