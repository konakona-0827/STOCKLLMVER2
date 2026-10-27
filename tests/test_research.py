from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from support_scout.agents import validate_reviews
from support_scout.contracts import DataError, Rules, calendar_at, load_series, timestamp
from support_scout.demo import demo_bundle
from support_scout.pipeline import run
from support_scout.support import detect_support


def first(report):
    return report["securities"][0]


def reviews_for(packet, verdict="RESEARCH"):
    return [{"packet_id": packet["packet_id"], "role": role, "agent_id": "test-" + role,
             "verdict": verdict, "evidence_ids": packet["core"]["required_confirmation_evidence"],
             "summary": "測試替身：支撐跌破即失效；未呼叫模型。", "risks": ["未驗證樣本外效果"],
             "missing_data": [], "produced_at": packet["core"]["as_of"]}
            for role in ("researcher", "reviewer")]


class DataContractTests(unittest.TestCase):
    def setUp(self):
        self.bundle = demo_bundle()

    def assertRejected(self, code):
        result = first(run(self.bundle))
        self.assertEqual(result["data_status"], "REJECTED")
        self.assertIn(code, result["reasons"][0])

    def test_intraday_snapshots_are_rejected(self):
        self.bundle["securities"][0]["interval"] = "1m_snapshot"
        self.assertRejected("DAILY_SOURCE")

    def test_japan_is_out_of_scope(self):
        self.bundle["securities"][0]["symbol"] = "6857.T"
        self.assertRejected("TAIWAN_SYMBOL")

    def test_duplicate_dates_are_not_deduplicated_silently(self):
        rows = self.bundle["securities"][0]["bars"]
        rows[-1] = deepcopy(rows[-2])
        self.assertRejected("DUPLICATE")

    def test_missing_session_and_stale_tail_are_rejected(self):
        self.bundle["securities"][0]["bars"].pop(-3)
        self.assertRejected("MISSING_OR_STALE")
        self.bundle = demo_bundle()
        self.bundle["securities"][0]["bars"].pop()
        self.assertRejected("MISSING_OR_STALE")

    def test_non_monotonic_availability_is_rejected(self):
        rows = self.bundle["securities"][0]["bars"]
        rows[1]["available_at"] = rows[10]["available_at"]
        self.assertRejected("NON_MONOTONIC")

    def test_invalid_price_nan_and_bool_are_rejected(self):
        for value in (float("nan"), float("inf"), True, 0, -1):
            with self.subTest(value=value):
                self.bundle = demo_bundle()
                self.bundle["securities"][0]["bars"][-1]["close"] = value
                self.assertRejected("INVALID_NUMBER")

    def test_bad_ohlc_is_rejected(self):
        self.bundle["securities"][0]["bars"][-1]["low"] = 200
        self.assertRejected("INVALID_OHLC")

    def test_incomplete_and_unknown_tradability_rejected(self):
        self.bundle["securities"][0]["bars"][-1]["complete"] = False
        self.assertRejected("INCOMPLETE")
        self.bundle = demo_bundle()
        self.bundle["securities"][0]["bars"][-1].pop("limit_locked")
        self.assertRejected("TRADABILITY_FLAGS")

    def test_unadjusted_actions_and_lot_units_rejected(self):
        self.bundle["securities"][0]["actions_reviewed"] = False
        self.assertRejected("CORPORATE_ACTION")
        self.bundle = demo_bundle()
        self.bundle["securities"][0]["volume_unit"] = "lots"
        self.assertRejected("SHARE_UNITS")

    def test_halts_and_zero_volume_are_not_filled_forward(self):
        self.bundle["securities"][0]["bars"][-5]["halted"] = True
        self.assertRejected("HALT_OR_ZERO_VOLUME")

    def test_closed_at_cannot_be_morning_timestamp(self):
        row = self.bundle["securities"][0]["bars"][-1]
        row["closed_at"] = row["session"] + "T09:00:00+08:00"
        self.assertRejected("INVALID_BAR_AVAILABILITY")

    def test_duplicate_symbols_both_rejected(self):
        self.bundle["securities"][1]["symbol"] = "9001"
        items = run(self.bundle)["securities"][:2]
        self.assertTrue(all(i["data_status"] == "REJECTED" for i in items))

    def test_calendar_missing_or_not_known_is_global_error(self):
        self.bundle["calendar"]["available_at"] = "2099-01-01T00:00:00+08:00"
        with self.assertRaisesRegex(DataError, "CALENDAR_NOT_AVAILABLE"):
            run(self.bundle)


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.bundle = demo_bundle()

    def test_scenarios(self):
        report = run(self.bundle)
        self.assertEqual([x.get("technical", {}).get("state") for x in report["securities"]],
                         ["RULE_CONFIRMED", "BOUNCE_SEEN", "INVALIDATED", "RULE_CONFIRMED", "RULE_CONFIRMED", None])
        self.assertEqual(report["technical_queue"], ["9001", "9005"])
        self.assertEqual(report["reviewed_shortlist"], [])

    def test_pivot_only_available_after_right_sessions(self):
        rows = self.bundle["securities"][0]["bars"]
        before = run(self.bundle, as_of=rows[63]["available_at"])
        known = run(self.bundle, as_of=rows[64]["available_at"])
        self.assertEqual(first(before)["technical"]["state"], "NO_SETUP")
        t = first(known)["technical"]
        self.assertEqual(t["state"], "BASE_FORMING")
        self.assertEqual(t["support"]["anchor_at"], rows[62]["session"])
        self.assertEqual(t["evidence"][0]["available_at"], rows[64]["available_at"])

    def test_ordered_different_session_transitions(self):
        rows = self.bundle["securities"][0]["bars"]
        expected = {64: "BASE_FORMING", 65: "BOUNCE_SEEN", 66: "BOUNCE_SEEN", 67: "RETEST_HELD", 68: "RULE_CONFIRMED"}
        for i, state in expected.items():
            self.assertEqual(first(run(self.bundle, as_of=rows[i]["available_at"]))["technical"]["state"], state)

    def test_sideways_bottom_is_not_retest_confirmation(self):
        for row in self.bundle["securities"][0]["bars"][65:]:
            row.update(open=102, high=104, low=101, close=103)
        self.assertEqual(first(run(self.bundle))["technical"]["state"], "BASE_FORMING")

    def test_same_day_retest_and_break_above_high_not_allowed(self):
        row = self.bundle["securities"][0]["bars"][67]
        row.update(open=103, high=113, low=100.5, close=112)
        report = run(self.bundle, as_of=row["available_at"])
        self.assertEqual(first(report)["technical"]["state"], "RETEST_HELD")

    def test_break_does_not_lower_frozen_support(self):
        rows = self.bundle["securities"][2]["bars"]
        before = run(self.bundle, as_of=rows[-2]["available_at"])["securities"][2]["technical"]
        after = run(self.bundle)["securities"][2]["technical"]
        self.assertEqual(after["state"], "INVALIDATED")
        self.assertEqual(before["support"]["lower"], after["support"]["lower"])
        self.assertEqual(after["support"]["anchor"], 100)
        self.assertIsNone(after["priority_score"])

    def test_extended_and_locked_cannot_be_candidates(self):
        report = run(self.bundle)
        self.assertFalse(report["securities"][3]["technical"]["technical_candidate"])
        self.bundle["securities"][0]["bars"][-1]["limit_locked"] = True
        self.assertIn("LIMIT_LOCKED", first(run(self.bundle))["technical"]["blockers"])

    def test_low_turnover_and_close_back_in_zone_not_candidates(self):
        for row in self.bundle["securities"][0]["bars"]:
            row["turnover_twd"] = 1000
        self.assertIn("LOW_20_SESSION_TURNOVER", first(run(self.bundle))["technical"]["blockers"])
        self.bundle = demo_bundle()
        self.bundle["securities"][0]["bars"][-1].update(open=101, high=103, low=100, close=100.8)
        t = first(run(self.bundle))["technical"]
        self.assertEqual(t["state"], "RULE_CONFIRMED")
        self.assertIn("LATEST_CLOSE_NOT_ABOVE_ZONE", t["blockers"])

    def test_future_append_and_prefix_outputs_identical(self):
        rows = self.bundle["securities"][0]["bars"]
        for i in range(60, 72):
            cutoff = rows[i]["available_at"]
            full = run(self.bundle, as_of=cutoff)
            prefix = deepcopy(self.bundle)
            for security in prefix["securities"]:
                security["bars"] = security["bars"][:i + 1]
            self.assertEqual(full, run(prefix, as_of=cutoff))
            for item in full["securities"]:
                for evidence in item.get("technical", {}).get("evidence", []):
                    self.assertLessEqual(timestamp(evidence["available_at"]), timestamp(cutoff))

    def test_short_history_explicit(self):
        for item in self.bundle["securities"]:
            item["bars"] = item["bars"][-22:]
        t = first(run(self.bundle))["technical"]
        self.assertEqual(t["state"], "INSUFFICIENT_DATA")
        self.assertFalse(t["technical_candidate"])

    def test_rules_are_validated_and_versioned(self):
        with self.assertRaises(ValueError):
            Rules(pivot_right=0)
        with self.assertRaises(ValueError):
            Rules(min_bars=1)
        self.assertNotEqual(first(run(self.bundle))["packet_id"],
                            first(run(self.bundle, rules=replace(Rules(), min_drawdown=0.09)))["packet_id"])


class AgentGateTests(unittest.TestCase):
    def setUp(self):
        self.bundle = demo_bundle()
        self.report = run(self.bundle)
        self.packet = self.report["agent_packets"][0]
        self.reviews = reviews_for(self.packet)

    def test_two_supported_reviews_promote_only_research(self):
        report = run(self.bundle, reviews=self.reviews)
        self.assertEqual(report["reviewed_shortlist"], ["9001"])
        self.assertEqual(first(report)["verdict"], "RESEARCH")
        self.assertNotIn("BUY", json.dumps(report))

    def test_reviewer_can_veto(self):
        self.reviews[1]["verdict"] = "REJECT"
        self.assertEqual(first(run(self.bundle, reviews=self.reviews))["verdict"], "REJECT")

    def test_one_agent_same_identity_or_watch_does_not_promote(self):
        self.assertEqual(first(run(self.bundle, reviews=self.reviews[:1]))["verdict"], "WATCH")
        self.reviews[1]["agent_id"] = self.reviews[0]["agent_id"]
        self.assertEqual(first(run(self.bundle, reviews=self.reviews))["verdict"], "WATCH")
        self.reviews = reviews_for(self.packet)
        self.reviews[1]["verdict"] = "WATCH"
        self.assertEqual(first(run(self.bundle, reviews=self.reviews))["verdict"], "WATCH")

    def test_invented_refs_extra_fields_and_missing_evidence_fail(self):
        for mutation in ("invented", "extra", "missing", "unknown", "bad_role"):
            with self.subTest(mutation=mutation):
                reviews = reviews_for(self.packet)
                if mutation == "invented": reviews[1]["evidence_ids"].append("MADE_UP")
                if mutation == "extra": reviews[1]["confidence"] = 0.99
                if mutation == "missing": reviews[1]["evidence_ids"] = reviews[1]["evidence_ids"][:1]
                if mutation == "unknown": reviews[1]["missing_data"] = ["事件未知"]
                if mutation == "bad_role": reviews[1]["role"] = []
                self.assertEqual(first(run(self.bundle, reviews=reviews))["verdict"], "WATCH")

    def test_stale_packet_cannot_be_reused(self):
        self.bundle["securities"][0]["bars"][-1]["close"] = 107.5
        report = run(self.bundle, reviews=self.reviews)
        self.assertEqual(first(report)["verdict"], "WATCH")
        self.assertTrue(report["unmatched_review_packets"])

    def test_duplicate_role_cannot_hide_rejection(self):
        duplicate = deepcopy(self.reviews[1])
        duplicate["verdict"] = "REJECT"
        result = first(run(self.bundle, reviews=self.reviews + [duplicate]))
        self.assertEqual(result["verdict"], "WATCH")
        self.assertIn("DUPLICATE_AGENT_ROLE", result["agent_errors"])

    def test_hard_data_gate_and_invalidated_support_override_agents(self):
        packet = next(p for p in self.report["agent_packets"] if p["core"]["symbol"] == "9004")
        report = run(self.bundle, reviews=reviews_for(packet))
        self.assertEqual(report["securities"][3]["verdict"], "WATCH")
        self.assertEqual(report["securities"][2]["verdict"], "REJECT")

    def test_unknown_thesis_point_in_time_and_near_event_remain_watch(self):
        for kind in ("thesis", "pit", "event"):
            bundle = demo_bundle()
            if kind == "thesis": bundle["securities"][0]["dossier"]["thesis_status"] = "UNKNOWN"
            if kind == "pit": bundle["securities"][0]["point_in_time"] = False
            if kind == "event":
                bundle["securities"][0]["dossier"]["events"] = [{"source": "SYNTHETIC_EVENT", "announced_at": bundle["as_of"],
                    "event_at": (timestamp(bundle["as_of"]) + timedelta(days=1)).isoformat(), "kind": "earnings"}]
            packet = run(bundle)["agent_packets"][0]
            result = first(run(bundle, reviews=reviews_for(packet)))
            self.assertEqual(result["verdict"], "WATCH")

    def test_broken_thesis_rejects(self):
        self.bundle["securities"][0]["dossier"]["thesis_status"] = "BROKEN"
        self.assertEqual(first(run(self.bundle))["verdict"], "REJECT")

    def test_review_cannot_predate_snapshot(self):
        self.reviews[0]["produced_at"] = "2025-01-01T12:00:00+08:00"
        result = first(run(self.bundle, reviews=self.reviews))
        self.assertIn("REVIEW_PREDATES_SNAPSHOT", result["agent_errors"])
        self.assertEqual(result["verdict"], "WATCH")


class CLITests(unittest.TestCase):
    def test_demo_creates_report_tasks_and_reproducible_input(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable, "-m", "support_scout", "demo", "--out", directory],
                                     capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(process.returncode, 0, process.stderr)
            output = Path(directory)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(report["synthetic"])
            self.assertTrue((output / "agent_tasks.json").is_file())
            self.assertEqual(report, run(json.loads((output / "input.json").read_text(encoding="utf-8"))))

    def test_all_bad_data_exit_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = demo_bundle()
            bundle["securities"] = [bundle["securities"][-1]]
            (root / "input.json").write_text(json.dumps(bundle), encoding="utf-8")
            process = subprocess.run([sys.executable, "-m", "support_scout", "scan", str(root / "input.json"), "--out", str(root / "out")],
                                     capture_output=True)
            self.assertEqual(process.returncode, 2)


if __name__ == "__main__":
    unittest.main()
