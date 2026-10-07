from __future__ import annotations

import unittest
from decimal import Decimal

from execution.capital_executor import (
    CapitalExecutionError,
    extract_seq13,
    parse_send_result,
    validate_real_order,
)
from execution.models import Side


class CapitalExecutorOfflineTests(unittest.TestCase):
    def test_extract_seq13(self):
        self.assertEqual("1234567890123", extract_seq13("ok 1234567890123 done"))

    def test_parse_tuple_send_result(self):
        result = parse_send_result(("1234567890123", 0))
        self.assertEqual(0, result.return_code)
        self.assertEqual("1234567890123", result.seq13)

    def test_parse_scalar_send_result(self):
        result = parse_send_result(0)
        self.assertEqual(0, result.return_code)
        self.assertIsNone(result.seq13)

    def test_validate_buy(self):
        sym, side, qty, px = validate_real_order("0050", "BUY", 1, "100.5")
        self.assertEqual("0050", sym)
        self.assertEqual(Side.BUY, side)
        self.assertEqual(1, qty)
        self.assertEqual(Decimal("100.5"), px)

    def test_validate_sell(self):
        _, side, _, _ = validate_real_order("0050", "SELL", 5, "100")
        self.assertEqual(Side.SELL, side)

    def test_reject_invalid_qty(self):
        with self.assertRaises(CapitalExecutionError):
            validate_real_order("0050", "BUY", 1000, "100")


if __name__ == "__main__":
    unittest.main()
