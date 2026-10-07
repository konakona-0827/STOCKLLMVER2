from __future__ import annotations

import unittest
from decimal import Decimal

from execution.advice_adapter import build_advice_intents
from execution.advice_sell_runner import plan_sell_orders_from_advice
from execution.risk_guard import RiskRejected


def payload(decision="SELL", ai_qty=7, action_ratio=0, suggested_qty=1):
    return {
        "interface_version": "1.0",
        "run_id": "RUN-SELL-1",
        "run_status": "READY",
        "llm_validation": "VALID",
        "real_order_sent": False,
        "market": {
            "quotes": [
                {
                    "symbol": "2891",
                    "quote_status": "LIVE",
                    "price": 40.0,
                    "bid": 39.95,
                    "ask": 40.0,
                    "age_seconds": 1,
                }
            ]
        },
        "recommendations": [
            {
                "symbol": "2891",
                "decision": decision,
                "confidence": 80,
                "reference_price": 40.0,
                "suggested_price": 39.95,
                "suggested_qty": suggested_qty,
                "action_ratio": action_ratio,
                "reason": "test",
                "warnings": [],
                "analysis_source": "POSITION",
                "ai_managed_qty": ai_qty,
                "quote_status": "LIVE",
                # no timestamp: test uses stored age_seconds
                "quote_timestamp": None,
                "quote_age_seconds": 1,
            }
        ],
    }


class AdviceSellRunnerTests(unittest.TestCase):
    def test_sell_one_of_seven_ai_managed(self):
        p = payload()
        intents = build_advice_intents(p)
        planned = plan_sell_orders_from_advice(p, intents)
        self.assertEqual(1, len(planned))
        self.assertEqual("2891", planned[0].plan.symbol)
        self.assertEqual(1, planned[0].plan.quantity)
        self.assertEqual(Decimal("39.95"), planned[0].plan.limit_price)

    def test_buy_is_ignored(self):
        p = payload(decision="BUY")
        intents = build_advice_intents(p)
        planned = plan_sell_orders_from_advice(p, intents)
        self.assertEqual([], planned)

    def test_sell_blocked_when_ai_managed_zero(self):
        p = payload(ai_qty=0)
        intents = build_advice_intents(p)
        with self.assertRaises(RiskRejected):
            plan_sell_orders_from_advice(p, intents)

    def test_sell_cannot_exceed_ai_managed(self):
        p = payload(ai_qty=7, suggested_qty=8)
        intents = build_advice_intents(p)
        with self.assertRaises(RiskRejected):
            plan_sell_orders_from_advice(p, intents)


if __name__ == "__main__":
    unittest.main()
