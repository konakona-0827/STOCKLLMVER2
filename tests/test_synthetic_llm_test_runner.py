from __future__ import annotations

import unittest
from decimal import Decimal

from execution.models import Side
from execution.synthetic_llm_test_runner import SyntheticSignalRequest


class SyntheticRunnerTests(unittest.TestCase):
    def test_buy_request(self):
        x = SyntheticSignalRequest(
            symbol="0050",
            side=Side.BUY,
            qty=1,
            price=Decimal("150"),
        )
        self.assertEqual(Side.BUY, x.side)
        self.assertEqual(1, x.qty)

    def test_sell_request(self):
        x = SyntheticSignalRequest(
            symbol="2891",
            side=Side.SELL,
            qty=1,
            price=Decimal("150"),
            bootstrap_ai_managed_qty=1,
        )
        self.assertEqual(Side.SELL, x.side)
        self.assertEqual(1, x.bootstrap_ai_managed_qty)


if __name__ == "__main__":
    unittest.main()
