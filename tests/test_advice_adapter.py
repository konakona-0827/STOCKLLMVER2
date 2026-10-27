from __future__ import annotations

import unittest

from execution.advice_adapter import AdviceInterfaceError, build_advice_intents


def base_payload():
    return {
        "interface_version": "1.0",
        "run_id": "RUN-001",
        "run_status": "READY",
        "llm_validation": "VALID",
        "real_order_sent": False,
        "recommendations": [
            {
                "symbol": "0050",
                "decision": "BUY",
                "confidence": 0.70,
                "reference_price": 116.2,
                "suggested_price": 116.1,
                "suggested_qty": 20,
                "action_ratio": 0,
                "reason": "test",
                "warnings": [],
                "analysis_source": "TOP10",
                "ai_managed_qty": 0,
                "quote_status": "LIVE",
                "quote_timestamp": "2026-10-08T09:30:00+08:00",
                "quote_age_seconds": 1.5,
            },
            {
                "symbol": "2330",
                "decision": "WAIT",
                "confidence": 80,
                "reason": "wait",
                "warnings": [],
                "quote_status": "LIVE",
            },
        ],
    }


class AdviceAdapterTests(unittest.TestCase):
    def test_buy_becomes_intent_wait_does_not(self):
        intents = build_advice_intents(base_payload())
        self.assertEqual(1, len(intents))
        self.assertEqual("0050", intents[0].symbol)
        self.assertEqual("BUY", intents[0].side.value)
        self.assertEqual("70.00", str(intents[0].confidence_pct))
        self.assertEqual("RUN-001:0050", intents[0].decision_id)

    def test_invalid_run_is_blocked(self):
        payload = base_payload()
        payload["run_status"] = "ERROR"
        with self.assertRaises(AdviceInterfaceError):
            build_advice_intents(payload)

    def test_real_order_already_sent_is_blocked(self):
        payload = base_payload()
        payload["real_order_sent"] = True
        with self.assertRaises(AdviceInterfaceError):
            build_advice_intents(payload)

    def test_duplicate_trade_symbol_is_blocked(self):
        payload = base_payload()
        payload["recommendations"].append(
            {
                "symbol": "0050",
                "decision": "SELL",
                "confidence": 80,
                "reason": "conflict",
                "warnings": [],
                "quote_status": "LIVE",
                "action_ratio": 0.5,
                "ai_managed_qty": 20,
            }
        )
        with self.assertRaises(AdviceInterfaceError):
            build_advice_intents(payload)


if __name__ == "__main__":
    unittest.main()
