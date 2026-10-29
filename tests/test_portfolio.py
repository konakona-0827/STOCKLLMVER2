"""Manual reports and holding suggestions, using only fabricated fills/prices."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from support_scout.contracts import DataError, digest
from support_scout.japan_demo import demo_bundle
from support_scout.pipeline import run
from support_scout.portfolio import (attach_manual_actions, load_fills, read_snapshot,
    record_fill, snapshot, verify_snapshot)


def fill(identifier="fill0001", side="BUY", quantity=100, price=100, *, confirmed=True,
         filled_at="2026-09-01T10:00:00+09:00", risk=None):
    row = {"id": identifier, "symbol": "9001", "side": side, "quantity": quantity,
        "price": price, "fees": 0, "filled_at": filled_at, "confirmed": confirmed}
    if risk:
        row["risk_reference"] = deepcopy(risk)
    return row


def risk(channel="MANUAL", lower=98, *, anchor_at=None):
    return {"channel": channel, "lower": lower, "price_basis": "raw_no_actions_in_window",
        "defined_at": "2026-09-01T09:00:00+09:00", "anchor_at": anchor_at}


def holding_report(path, *, reference=None, views=("HOLD", "HOLD"), unknown_context=False,
                   filled_at="2026-09-01T10:00:00+09:00", recorded_at="2026-09-01T11:00:00+09:00"):
    bundle = demo_bundle()
    bundle["securities"] = bundle["securities"][:1]
    if unknown_context:
        bundle["securities"][0]["dossier"] = {}
    record_fill(path, fill(risk=reference, filled_at=filled_at), now=recorded_at)
    bundle["portfolio_snapshot"] = read_snapshot(path, bundle["as_of"])
    initial = run(bundle)
    packet = initial["agent_packets"][0]
    reviews = [{"packet_id": packet["packet_id"], "role": role, "agent_id": "test-"+role,
        "verdict": "RESEARCH", "evidence_ids": packet["core"]["required_confirmation_evidence"],
        "summary": "測試用持倉審查", "risks": [], "missing_data": [], "produced_at": bundle["as_of"],
        "position_view": {"action": action, "reason": "測試用持倉評語"}}
        for role, action in zip(("researcher", "reviewer"), views)]
    return run(bundle, reviews=reviews), bundle


class ManualLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"fills.json"

    def tearDown(self):
        self.temp.cleanup()

    def add(self, row, now="2026-09-01T11:00:00+09:00"):
        return record_fill(self.path, row, now=now)

    def test_unconfirmed_buys_and_sells_never_create_or_change_holding(self):
        self.add(fill(confirmed=False))
        self.add(fill("fill0002", "SELL", quantity=500, confirmed=False))
        value = read_snapshot(self.path, "2026-09-01T12:00:00+09:00")
        self.assertEqual(value["positions"], {})
        self.assertEqual(value["unconfirmed_fills_ignored"], 2)
        self.assertEqual(value["confirmed_fill_count"], 0)

    def test_duplicate_id_is_idempotent_but_different_payload_is_conflict(self):
        row = self.add(fill())
        again = self.add(fill(), "2026-09-02T11:00:00+09:00")
        self.assertEqual(row, again)
        self.assertEqual(len(load_fills(self.path)["fills"]), 1)
        with self.assertRaisesRegex(DataError, "IDEMPOTENCY_CONFLICT"):
            self.add(fill(price=101))

    def test_cost_includes_fees_and_partial_sale_keeps_average_cost(self):
        first = fill(); first["fees"] = 100
        self.add(first)
        self.add(fill("fill0002", quantity=100, price=120), "2026-09-01T12:00:00+09:00")
        self.add(fill("fill0003", "SELL", quantity=50, price=140,
            filled_at="2026-09-01T13:00:00+09:00"), "2026-09-01T14:00:00+09:00")
        value = read_snapshot(self.path, "2026-09-01T15:00:00+09:00")["positions"]["9001.T"]
        self.assertEqual(value["quantity"], 150)
        self.assertEqual(value["average_cost"], 110.5)
        self.assertEqual(value["total_cost"], 16575)

    def test_full_close_and_reopen_reset_opened_date_and_risk(self):
        self.add(fill(risk=risk()))
        self.add(fill("fill0002", "SELL", filled_at="2026-09-02T10:00:00+09:00"), "2026-09-02T11:00:00+09:00")
        self.assertFalse(read_snapshot(self.path, "2026-09-02T12:00:00+09:00")["positions"])
        self.add(fill("fill0003", price=130, filled_at="2026-09-03T10:00:00+09:00"), "2026-09-03T11:00:00+09:00")
        position = read_snapshot(self.path, "2026-09-03T12:00:00+09:00")["positions"]["9001.T"]
        self.assertEqual(position["opened_at"], "2026-09-03T10:00:00+09:00")
        self.assertEqual(position["average_cost"], 130)
        self.assertIsNone(position["risk_reference"])

    def test_cutoff_requires_fill_and_recording_time_both_visible(self):
        self.add(fill(), "2026-09-03T11:00:00+09:00")
        self.assertFalse(read_snapshot(self.path, "2026-09-02T12:00:00+09:00")["positions"])
        after = read_snapshot(self.path, "2026-09-03T12:00:00+09:00")
        self.assertEqual(after["confirmed_fill_count"], 1)
        self.assertEqual(verify_snapshot(after)["positions"]["9001.T"]["quantity"], 100)

    def test_oversell_and_backdated_sale_before_buy_do_not_write(self):
        self.add(fill())
        for row in [fill("fill0002", "SELL", quantity=101),
                    fill("fill0003", "SELL", filled_at="2026-08-31T10:00:00+09:00")]:
            with self.assertRaisesRegex(DataError, "SELL_EXCEEDS"):
                self.add(row, "2026-09-02T12:00:00+09:00")
        self.assertEqual(len(load_fills(self.path)["fills"]), 1)

    def test_invalid_bool_nan_negative_and_unknown_fields_are_rejected(self):
        for key, value in [("quantity", True), ("quantity", 0), ("price", True), ("price", float("nan")),
                           ("fees", -1), ("confirmed", 1), ("side", []), ("filled_at", "2026-09-01")]:
            row = fill(); row[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(DataError):
                self.add(row)
        row = fill(); row["unexpected"] = "do not accept"
        with self.assertRaises(DataError): self.add(row)

    def test_future_fill_and_future_risk_definition_are_rejected(self):
        with self.assertRaisesRegex(DataError, "CANNOT_BE_IN_FUTURE"):
            self.add(fill(filled_at="2026-09-02T10:00:00+09:00"))
        future = risk(); future["defined_at"] = "2026-09-02T10:00:00+09:00"
        with self.assertRaisesRegex(DataError, "RISK_NOT_YET_KNOWN"):
            self.add(fill(risk=future))

    def test_conflicting_later_buy_risk_is_not_silently_redrawn(self):
        self.add(fill(risk=risk(lower=98)))
        self.add(fill("fill0002", risk=risk(lower=90)), "2026-09-02T11:00:00+09:00")
        position = read_snapshot(self.path, "2026-09-02T12:00:00+09:00")["positions"]["9001.T"]
        self.assertEqual(position["risk_reference_status"], "CONFLICT_REVIEW")
        self.assertIsNone(position["risk_reference"])
        self.assertEqual(position["quantity"], 200)

    def test_snapshot_recomputes_holdings_even_if_attacker_rehashes_body(self):
        self.add(fill())
        value = read_snapshot(self.path, "2026-09-01T12:00:00+09:00")
        value["positions"]["9001.T"]["quantity"] = 900
        value["digest"] = digest({k:v for k,v in value.items() if k != "digest"})
        with self.assertRaisesRegex(DataError, "SNAPSHOT_INTEGRITY"):
            verify_snapshot(value)
        value = read_snapshot(self.path, "2026-09-01T12:00:00+09:00")
        with self.assertRaisesRegex(DataError, "CUTOFF_MISMATCH"):
            verify_snapshot(value, "2026-09-01T13:00:00+09:00")


class ManualActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"fills.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_risk_basis_never_turns_terminal_atr_episode_into_sell(self):
        report, _ = holding_report(self.path)
        item = report["securities"][0]
        self.assertEqual(item["manual_action"]["action"], "REVIEW")
        item["technical"]["state"] = "INVALIDATED"
        attach_manual_actions(report)
        self.assertEqual(item["manual_action"]["action"], "REVIEW")
        self.assertFalse(item["manual_action"]["execution_enabled"])

    def test_valid_manual_threshold_breach_is_exit_review_without_order(self):
        report, _ = holding_report(self.path, reference=risk(lower=110))
        action = report["securities"][0]["manual_action"]
        self.assertEqual(action["action"], "EXIT_REVIEW")
        self.assertIn("USER_RECORDED_RISK_LEVEL_BREACHED_AT_DAILY_CLOSE", action["reasons"])
        self.assertFalse(action["filled"])

    def test_quote_before_new_fill_cannot_trigger_exit_from_old_close(self):
        report, _ = holding_report(self.path, reference=risk(lower=110),
            filled_at="2026-09-08T15:45:00+09:00", recorded_at="2026-09-08T15:50:00+09:00")
        action = report["securities"][0]["manual_action"]
        self.assertEqual(action["action"], "REVIEW")
        self.assertIn("PRICE_PREDATES_LAST_CONFIRMED_FILL", action["reasons"])

    def test_both_valid_reviews_and_known_risk_can_propose_hold(self):
        report, _ = holding_report(self.path, reference=risk())
        self.assertEqual(report["securities"][0]["manual_action"]["action"], "HOLD")

    def test_unknown_context_and_stale_ledger_cannot_approve_hold(self):
        report, _ = holding_report(self.path, reference=risk())
        report["ledger"] = {"rows": [{"symbol": "9001.T", "change": "STALE_IGNORED"}]}
        attach_manual_actions(report)
        self.assertEqual(report["securities"][0]["manual_action"]["action"], "REVIEW")

    def test_unknown_company_context_is_not_hold_approval(self):
        report, _ = holding_report(self.path, reference=risk(), unknown_context=True)
        action = report["securities"][0]["manual_action"]
        self.assertEqual(action["action"], "REVIEW")
        self.assertIn("HOLDING_INFORMATION_OR_AGENT_CONSENSUS_INCOMPLETE", action["reasons"])
        report["ledger"] = {}
        report["collection"] = {"status": "DEGRADED"}
        attach_manual_actions(report)
        self.assertEqual(report["securities"][0]["manual_action"]["action"], "REVIEW")

    def test_valid_agent_exit_view_escalates_to_human_review(self):
        report, _ = holding_report(self.path, reference=risk(), views=("HOLD", "EXIT_REVIEW"))
        action = report["securities"][0]["manual_action"]
        self.assertEqual(action["action"], "EXIT_REVIEW")
        self.assertIn("POSITION_AGENT_DISAGREEMENT", action["reasons"])

    def test_holdings_not_observed_stay_in_manual_action_list(self):
        report, _ = holding_report(self.path, reference=risk())
        report["securities"] = []
        attach_manual_actions(report)
        self.assertEqual(report["manual_actions"][0]["symbol"], "9001.T")
        self.assertEqual(report["manual_actions"][0]["position"]["quantity"], 100)
        self.assertEqual(report["manual_actions"][0]["reasons"], ["PORTFOLIO_SYMBOL_NOT_OBSERVED"])

    def test_h001_and_atr_risk_channels_cannot_be_mixed(self):
        report, _ = holding_report(self.path, reference=risk("H-001", 98, anchor_at="2026-08-26"))
        action = report["securities"][0]["manual_action"]
        self.assertEqual(action["action"], "REVIEW")
        self.assertIn("POSITION_RISK_BASIS_MISMATCH", action["reasons"])


if __name__ == "__main__":
    unittest.main()
