from __future__ import annotations

import unittest
from decimal import Decimal

from execution.account_guard import (
    cash_sellable_qty,
    parse_get_balance,
    parse_real_balance_row,
)


class AccountGuardTests(unittest.TestCase):
    def test_parse_get_balance(self):
        x = parse_get_balance("true,10000,9000,8000.")
        self.assertTrue(x.verified)
        self.assertEqual(Decimal("8000"), x.available_to_buy_twd)

    def test_non_one_account_not_verified(self):
        x = parse_get_balance("false,0,0,0.")
        self.assertFalse(x.verified)

    def test_parse_real_balance(self):
        raw = "2891,T,0,0,0,0,7,0,0,0,0,7,0,0,7,X,0,LOGIN,ACC"
        x = parse_real_balance_row(raw)
        self.assertIsNotNone(x)
        self.assertEqual("2891", x.symbol)
        self.assertEqual(7, x.sellable_qty)
        self.assertEqual(7, x.realtime_qty)

    def test_sellable_cash_qty(self):
        a = parse_real_balance_row(
            "2891,T,0,0,0,0,7,0,0,0,0,7,0,0,7,X,0,LOGIN,ACC"
        )
        b = parse_real_balance_row(
            "2891,C,0,0,0,0,3,0,0,0,0,3,0,0,3,X,0,LOGIN,ACC"
        )
        self.assertEqual(7, cash_sellable_qty([a, b], "2891"))


if __name__ == "__main__":
    unittest.main()
