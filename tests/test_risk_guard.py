from __future__ import annotations

import unittest
from decimal import Decimal

from execution.models import AdviceIntent, Side
from execution.risk_guard import (
    MarketQuote,
    PositionState,
    RiskConfig,
    RiskRejected,
    build_order_plan,
)


def intent(
    side: Side,
    *,
    suggested_qty=None,
    action_ratio="0",
    ai_managed_qty=0,
    amount_twd=None,
):
    return AdviceIntent(
        run_id="RUN-1",
        decision_id="RUN-1:0050",
        interface_version="1.0",
        symbol="0050",
        side=side,
        confidence_pct=Decimal("80"),
        reason="test",
        analysis_source="TOP10",
        warnings=(),
        quote_status="LIVE",
        quote_timestamp="2026-10-08T09:30:00+08:00",
        quote_age_seconds=Decimal("1"),
        reference_price=Decimal("116"),
        suggested_price=Decimal("116"),
        suggested_qty=suggested_qty,
        action_ratio=Decimal(action_ratio),
        ai_managed_qty=ai_managed_qty,
        evidence_ids=(),
        amount_twd=None if amount_twd is None else Decimal(str(amount_twd)),
    )


def quote(*, status="LIVE", age="1", bid="115.9", ask="116.1"):
    return MarketQuote(
        symbol="0050",
        quote_status=status,
        age_seconds=Decimal(age),
        last_price=Decimal("116"),
        bid=None if bid is None else Decimal(bid),
        ask=None if ask is None else Decimal(ask),
    )


class RiskGuardTests(unittest.TestCase):
    def test_buy_uses_ask_and_suggested_qty(self):
        plan = build_order_plan(
            intent(Side.BUY, suggested_qty=20),
            quote=quote(),
        )
        self.assertEqual(20, plan.quantity)
        self.assertEqual(Decimal("116.1"), plan.limit_price)
        self.assertEqual("suggested_qty", plan.sizing_source)

    def test_buy_can_derive_qty_from_amount(self):
        plan = build_order_plan(
            intent(Side.BUY, amount_twd=3000),
            quote=quote(),
        )
        self.assertEqual(25, plan.quantity)
        self.assertEqual("amount_twd", plan.sizing_source)

    def test_stale_quote_is_rejected(self):
        with self.assertRaises(RiskRejected):
            build_order_plan(
                intent(Side.BUY, suggested_qty=1),
                quote=quote(age="121"),
            )

    def test_non_live_quote_is_rejected(self):
        with self.assertRaises(RiskRejected):
            build_order_plan(
                intent(Side.BUY, suggested_qty=1),
                quote=quote(status="LAST_KNOWN"),
            )

    def test_sell_50pct_uses_ai_managed_qty_only(self):
        plan = build_order_plan(
            intent(Side.SELL, action_ratio="0.5", ai_managed_qty=20),
            quote=quote(),
            position=PositionState(symbol="0050", user_qty=10, ai_managed_qty=20),
        )
        self.assertEqual(10, plan.quantity)
        self.assertEqual(Decimal("115.9"), plan.limit_price)
        self.assertEqual("action_ratio", plan.sizing_source)

    def test_sell_cannot_touch_user_qty(self):
        with self.assertRaises(RiskRejected):
            build_order_plan(
                intent(Side.SELL, suggested_qty=25, ai_managed_qty=20),
                quote=quote(),
                position=PositionState(symbol="0050", user_qty=10, ai_managed_qty=20),
            )

    def test_missing_ask_is_rejected_for_buy(self):
        with self.assertRaises(RiskRejected):
            build_order_plan(
                intent(Side.BUY, suggested_qty=1),
                quote=quote(ask=None),
            )

    def test_order_value_limit_is_enforced(self):
        cfg = RiskConfig(max_order_twd=Decimal("2000"))
        with self.assertRaises(RiskRejected):
            build_order_plan(
                intent(Side.BUY, suggested_qty=20),
                quote=quote(),
                config=cfg,
            )


if __name__ == "__main__":
    unittest.main()
