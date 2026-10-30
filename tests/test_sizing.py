"""Sizing/cash/risk controls with invented signals, never orders or mail."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from support_scout.contracts import DataError
from support_scout.sizing import EXAMPLE, load_settings, attach_execution_plans
from support_scout.portfolio import snapshot


def settings(**changes):
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    value.update(capital_jpy=200000, per_trade_risk_fraction=.05)
    value.update(changes)
    return value


def report(count=1, action="PROBE_BUY_REVIEW"):
    return {"synthetic": False, "as_of": "2026-10-29T16:00:00+09:00", "securities": [
        {"symbol": f"{9001+i}.T", "name": "人造標的", "opportunity_decision": {"action": action, "label": "人造機會",
            "scope": "CONDITIONAL_MANUAL_REVIEW", "manual_checks": [], "reference_plan": {
                "entry_reference": 100, "invalidation_reference": 97, "target_reference": 115, "risk_fraction": .04}}}
        for i in range(count)]}


class SizingTests(unittest.TestCase):
    def test_capital_risk_and_position_caps_are_limits_not_targets(self):
        r = attach_execution_plans(report(), settings())
        p = r["securities"][0]["execution_plan"]
        self.assertEqual(p["suggested_amount_jpy"], 50000)
        self.assertAlmostEqual(p["estimated_loss_jpy"], 2250)
        self.assertLess(p["estimated_loss_jpy"], p["risk_budget_jpy"])
        self.assertLessEqual(p["suggested_shares"] * 100.5, 50000)

    def test_per_trade_risk_and_amount_rounding_never_exceed_budget(self):
        p = attach_execution_plans(report(), settings(per_trade_risk_fraction=.001))["securities"][0]["execution_plan"]
        self.assertEqual(p["suggested_amount_jpy"], 4000)
        self.assertLessEqual(p["estimated_loss_jpy"], 200)

    def test_multiple_candidates_share_cash_and_new_risk_in_search_order(self):
        r = report(6)
        r["discovery"] = {"search": {"selection": [{"symbol": "9003.T"}, {"symbol": "9001.T"}]}}
        attach_execution_plans(r, settings(max_new_risk_fraction=.01))
        self.assertEqual(r["decision_brief"]["rows"][0]["symbol"], "9003.T")
        self.assertEqual(r["securities"][2]["execution_plan"]["suggested_amount_jpy"], 44000)
        self.assertLessEqual(r["decision_brief"]["proposed_loss_scenario_jpy"], 2000)
        self.assertLessEqual(r["decision_brief"]["proposed_total_jpy"], 200000)
        self.assertEqual(sum(x["execution_plan"]["suggested_amount_jpy"] > 0 for x in r["securities"]), 1)

    def test_wait_avoid_missing_budget_and_invalid_references_never_buy(self):
        for action in ("WAIT", "AVOID"):
            self.assertEqual(attach_execution_plans(report(action=action), settings())["decision_brief"]["proposed_total_jpy"], 0)
        self.assertEqual(attach_execution_plans(report(), settings(capital_jpy=None))["decision_brief"]["proposed_total_jpy"], 0)
        for value in (None, True, float("nan"), 101):
            r = report(); r["securities"][0]["opportunity_decision"]["reference_plan"]["invalidation_reference"] = value
            self.assertEqual(attach_execution_plans(r, settings())["decision_brief"]["proposed_total_jpy"], 0)

    def test_broker_costs_can_block_a_signal_with_too_little_reward(self):
        r = report();r["securities"][0]["opportunity_decision"]["reference_plan"]["target_reference"] = 103
        p = attach_execution_plans(r, settings())["securities"][0]["execution_plan"]
        self.assertEqual(p["suggested_amount_jpy"], 0)
        self.assertEqual(p["sizing_status"], "BROKER_COST_RISK_REVIEW_REQUIRED")

    def test_board_lot_is_not_treated_as_fractional_amount_trade(self):
        r = report();p = r["securities"][0]["opportunity_decision"]["reference_plan"]
        p.update(entry_reference=31070, invalidation_reference=29729.7, target_reference=35420, risk_fraction=.0522)
        result = attach_execution_plans(r, settings(broker_mode="BOARD_LOT"))["securities"][0]["execution_plan"]
        self.assertEqual(result["suggested_amount_jpy"], 0)
        self.assertEqual(result["sizing_status"], "BELOW_MINIMUM_OR_LOT_UNAFFORDABLE")

    def test_confirmed_cash_flows_include_realized_loss_and_fees(self):
        r = report()
        fills = [{"id": "fill0001", "symbol": "9002.T", "side": "BUY", "quantity": 1, "price": 200, "fees": 0,
            "filled_at": "2026-10-28T10:00:00+09:00", "recorded_at": "2026-10-28T10:01:00+09:00", "sequence": 1, "confirmed": True},
            {"id": "fill0002", "symbol": "9002.T", "side": "SELL", "quantity": 1, "price": 100, "fees": 20,
            "filled_at": "2026-10-28T11:00:00+09:00", "recorded_at": "2026-10-28T11:01:00+09:00", "sequence": 2, "confirmed": True}]
        r["portfolio_snapshot"] = snapshot({"schema_version": 1, "fills": fills}, r["as_of"])
        brief = attach_execution_plans(r, settings(capital_jpy=1000))["decision_brief"]
        self.assertEqual(brief["available_cash_estimate_jpy"], 880)

    def test_existing_holding_does_not_get_automatic_add(self):
        r = report()
        fill = {"id": "fill0001", "symbol": "9001.T", "side": "BUY", "quantity": .5, "price": 100, "fees": 1,
            "filled_at": "2026-10-28T10:00:00+09:00", "recorded_at": "2026-10-28T10:01:00+09:00", "sequence": 1, "confirmed": True}
        r["portfolio_snapshot"] = snapshot({"schema_version": 1, "fills": [fill]}, r["as_of"])
        p = attach_execution_plans(r, settings())["securities"][0]["execution_plan"]
        self.assertEqual(p["suggested_amount_jpy"], 0)
        self.assertEqual(p["held_quantity"], .5)
        self.assertEqual(p["sizing_status"], "HOLDING_REVIEW_NO_ADD")

    def test_reconstruction_sizing_is_marked_simulation(self):
        r = report();r["securities"][0]["opportunity_decision"]["scope"] = "SIMULATION_ONLY"
        self.assertEqual(attach_execution_plans(r, settings())["securities"][0]["execution_plan"]["sizing_status"], "SIMULATION_ONLY")

    def test_invalid_settings_and_fabricated_portfolio_fail_closed(self):
        for changes in ({"capital_jpy": True}, {"per_trade_risk_fraction": 1.1}, {"amount_increment_jpy": 0}, {"entry_spread_fraction": float("nan")}):
            with self.assertRaises(DataError):load_settings(settings(**changes))
        r = report();r["portfolio_snapshot"] = {"positions": {}}
        self.assertEqual(attach_execution_plans(r, settings())["decision_brief"]["proposed_total_jpy"], 0)


if __name__ == "__main__":
    unittest.main()
