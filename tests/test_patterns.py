"""H-001 hypotheses and synthetic Advantest controls, never real market performance."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support_scout.agents import verify_packet
from support_scout.contracts import DataError, Rules, digest
from support_scout.decision import attach_decisions
from support_scout.discovery import SearchCompletion, make_search_packet
from support_scout.pattern_case import build_case_bundle, load_case, replay_case, run_case
from support_scout.patterns import H001Parameters, load_registry
from support_scout.pipeline import run
from support_scout.providers import Completion


def evaluated(checkpoint="support", case=None):
    bundle = build_case_bundle(case or load_case(), checkpoint)
    report = run(bundle)
    return report["securities"][0]["patterns"][0], report, bundle


class PatternTests(unittest.TestCase):
    def test_synthetic_case_separates_three_opportunity_stages_and_two_negative_controls(self):
        for checkpoint in load_case()["checkpoints"]:
            with self.subTest(checkpoint=checkpoint["id"]):
                pattern, report, _ = evaluated(checkpoint["id"])
                self.assertEqual(pattern["stage"], checkpoint["expected_stage"])
                self.assertNotEqual(report["securities"][0]["trade_decision"]["conclusion"], "CONSIDER")
                self.assertEqual(pattern["empirical_status"], "UNVALIDATED_HYPOTHESIS_NOT_WIN_PROBABILITY")

    def test_future_earnings_and_later_prices_do_not_change_pre_event_packet_or_pattern(self):
        original = load_case()
        changed = deepcopy(original)
        changed["daily_overrides"][-1].update(open=50000, high=55000, low=49000, close=54000)
        changed["pattern_context"]["catalysts"][-1]["comparison"]["actual"] = 9000
        before, first, _ = evaluated("early", original)
        after, second, _ = evaluated("early", changed)
        self.assertEqual(before, after)
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        packet = json.dumps(first["agent_packets"], ensure_ascii=False)
        self.assertNotIn("synthetic-earnings-surprise", packet)
        self.assertNotIn("hypothetical_eps", packet)
        self.assertEqual(before["metrics"]["positive_catalyst_evidence_ids"], [])

    def test_positive_news_without_pre_event_comparable_expectation_is_not_surprise(self):
        for mode in ("no_consensus", "late_consensus", "units", "below_expectation", "unverified"):
            case = load_case()
            event = case["pattern_context"]["catalysts"][-1]
            if mode == "no_consensus": event.pop("comparison")
            if mode == "late_consensus": event["comparison"]["expectation_available_at"] = "2026-10-28T16:00:00+09:00"
            if mode == "units": event["comparison"]["actual_unit"] = "million JPY"
            if mode == "below_expectation": event["comparison"]["actual"] = 90
            if mode == "unverified": event["verified"] = False
            with self.subTest(mode=mode):
                pattern, _, _ = evaluated("catalyst", case)
                self.assertEqual(pattern["stage"], "SUPPORT_STRENGTHENED")
                self.assertEqual(pattern["metrics"]["positive_catalyst_evidence_ids"], [])

    def test_surprise_without_volume_breakout_is_not_full_catalyst_signal(self):
        case = load_case()
        case["daily_overrides"][-1]["volume"] = 1000000
        pattern, _, _ = evaluated("catalyst", case)
        self.assertTrue(pattern["metrics"]["positive_catalyst_evidence_ids"])
        self.assertFalse(pattern["metrics"]["breakout"])
        self.assertEqual(pattern["stage"], "CATALYST_AWAITING_PRICE")

    def test_after_close_announcement_requires_a_new_post_event_daily_bar(self):
        pending, _, _ = evaluated("catalyst_announced")
        confirmed, _, _ = evaluated("catalyst")
        self.assertEqual(pending["stage"], "CATALYST_AWAITING_PRICE")
        self.assertTrue(pending["metrics"]["breakout"])
        self.assertFalse(pending["metrics"]["post_event_price_available"])
        self.assertIn("POST_EVENT_PRICE_CONFIRMATION_REQUIRED", pending["reason_codes"])
        self.assertTrue(confirmed["metrics"]["post_event_price_available"])
        self.assertEqual(confirmed["stage"], "CATALYST_CONFIRMED")

    def test_duplicate_visible_catalysts_cannot_hide_conflicting_evidence(self):
        case = load_case()
        duplicate = deepcopy(case["pattern_context"]["catalysts"][-1])
        duplicate["comparison"]["actual"] = 80
        case["pattern_context"]["catalysts"].append(duplicate)
        pattern, _, _ = evaluated("catalyst", case)
        self.assertEqual(pattern["metrics"]["positive_catalyst_evidence_ids"], [])
        self.assertIn("CATALYST_INPUT_INVALID", pattern["reason_codes"])

    def test_invalidated_episode_remains_invalid_when_no_new_setup_exists(self):
        case = load_case()
        checkpoint = deepcopy(case["checkpoints"][-1])
        checkpoint.update(id="broken_later", as_of="2026-10-29T16:00:00+09:00")
        case["checkpoints"].append(checkpoint)
        pattern, _, _ = evaluated("broken_later", case)
        self.assertEqual(pattern["stage"], "INVALIDATED")
        self.assertEqual(pattern["support"]["lower"], 29700)
        self.assertTrue(pattern["episodes"][-1]["terminal"])

    def test_price_extended_or_support_broken_is_not_a_research_candidate(self):
        for checkpoint in ("chase_control", "broken_control"):
            pattern, report, bundle = evaluated(checkpoint)
            self.assertFalse(pattern["research_candidate"])
            search = make_search_packet(report, bundle["source_universe"], max_selected=1)
            self.assertEqual(search["core"]["candidates"], [])

    def test_frozen_support_does_not_move_down_when_broken(self):
        support, _, _ = evaluated("support")
        broken, _, _ = evaluated("broken_control")
        self.assertEqual(support["support"], broken["support"])
        self.assertEqual(broken["stage"], "INVALIDATED")
        self.assertEqual(broken["support"]["lower"], 29700)

    def test_unverified_volume_basis_cannot_claim_strengthened_support(self):
        _, _, bundle = evaluated("support")
        bundle["observation_only"] = True
        bundle["securities"][0].update(observation_only=True, price_basis="raw_unverified",
            actions_reviewed=False, tradability_reviewed=False, volume_comparable=False, point_in_time=False)
        report = run(bundle)
        pattern = report["securities"][0]["patterns"][0]
        self.assertEqual(pattern["stage"], "EARLY_WATCH")
        self.assertIn("PATTERN_VOLUME_BASIS_UNVERIFIED", pattern["reason_codes"])
        self.assertNotEqual(report["securities"][0]["trade_decision"]["conclusion"], "CONSIDER")

    def test_fundamental_deterioration_is_a_pattern_counterexample(self):
        case = load_case()
        case["pattern_context"]["decline_driver"]["classification"] = "FUNDAMENTAL_DETERIORATION"
        pattern, _, _ = evaluated("support", case)
        self.assertEqual(pattern["stage"], "CONTRADICTED")
        self.assertFalse(pattern["research_candidate"])

    def test_disabled_pattern_and_parameter_changes_are_version_bound(self):
        _, original, bundle = evaluated()
        bundle["pattern_registry"]["definitions"][0]["parameters"]["volume_contraction_ratio"] = .7
        changed = run(bundle)
        self.assertNotEqual(changed["snapshot_id"], original["snapshot_id"])
        self.assertNotEqual(changed["pattern_registry_digest"], original["pattern_registry_digest"])
        bundle["pattern_registry"]["definitions"][0]["enabled"] = False
        self.assertEqual(run(bundle)["securities"][0]["patterns"], [])

    def test_invalid_parameters_and_unknown_registry_handlers_fail_explicitly(self):
        for kwargs in ({"stabilization_min_sessions": True}, {"min_decline_fraction": 1},
                       {"volume_contraction_ratio": 1.1}, {"breakout_volume_multiple": .5},
                       {"max_distance_fraction": float("nan")}, {"stabilization_min_sessions": 6}):
            with self.subTest(kwargs=kwargs), self.assertRaises(DataError):
                H001Parameters(**kwargs)
        registry = load_registry()
        registry["definitions"][0]["handler"] = "arbitrary_python_path"
        with self.assertRaises(DataError):
            load_registry(registry)

    def test_pattern_only_early_opportunity_reaches_search_without_strict_pivot_setup(self):
        bundle = build_case_bundle(load_case(), "support")
        report = run(bundle, rules=replace(Rules(), min_drawdown=.5))
        item = report["securities"][0]
        self.assertEqual(item["technical"]["state"], "NO_SETUP")
        self.assertEqual(len(report["agent_packets"]), 1)
        search = make_search_packet(report, bundle["source_universe"], max_selected=1)
        self.assertEqual(search["core"]["candidates"][0]["symbol"], "6857.T")
        self.assertTrue(search["core"]["candidates"][0]["patterns"][0]["research_candidate"])

    def test_mutating_pattern_breaks_packet_and_downstream_integrity(self):
        _, report, bundle = evaluated()
        packet = deepcopy(report["agent_packets"][0])
        packet["core"]["patterns"][0]["stage"] = "CATALYST_CONFIRMED"
        with self.assertRaises(DataError):
            verify_packet(packet)
        report["securities"][0]["patterns"][0]["stage"] = "CATALYST_CONFIRMED"
        attach_decisions(report, bundle)
        self.assertIn("SNAPSHOT_INTEGRITY_FAILED", report["securities"][0]["trade_decision"]["reason_codes"])


class SearchFixture:
    mode, model = "test_transport", "fake-search-model"
    def complete(self, packet):
        candidate = packet["core"]["candidates"][0]
        return SearchCompletion({"selections": [{"candidate_id": candidate["candidate_id"],
            "evidence_ids": candidate["required_evidence_ids"], "rationale": "僅驗證合成Pattern候選交接。"}]})


class ReviewFixture:
    mode, model = "test_transport", "fake-review-model"
    def __init__(self): self.seen = []
    def complete(self, packet, role):
        self.seen.append((role, deepcopy(packet)))
        return Completion({"verdict": "WATCH", "evidence_ids": packet["core"]["required_confirmation_evidence"],
            "summary": "合成案例參數研究，仍待驗證。", "risks": ["尚無樣本外證據"], "missing_data": ["真實行情驗證"]})


class PatternCaseTests(unittest.TestCase):
    def test_three_roles_receive_stage_and_parameters_without_future_catalysts(self):
        research, reviewer = ReviewFixture(), ReviewFixture()
        with tempfile.TemporaryDirectory() as folder, patch("support_scout.pattern_case.openai_config", side_effect=AssertionError("key read")):
            result, target = run_case(output_root=folder, agents=True, search_provider=SearchFixture(),
                researcher=research, reviewer=reviewer)
            self.assertEqual(result["model_call_count"], 12)
            self.assertEqual(len(research.seen), 4)
            self.assertEqual(len(reviewer.seen), 4)
            early = research.seen[0][1]
            self.assertNotIn("synthetic-earnings-surprise", json.dumps(early))
            self.assertEqual(early["core"]["patterns"][0]["stage"], "EARLY_WATCH")
            self.assertEqual(research.seen[2][1]["core"]["patterns"][0]["stage"], "CATALYST_AWAITING_PRICE")
            self.assertEqual(research.seen[3][1]["core"]["patterns"][0]["stage"], "CATALYST_CONFIRMED")
            self.assertTrue(all(r["trade_conclusion"] != "CONSIDER" for r in result["results"]))
            self.assertEqual(result["results"][-1]["status"], "SEARCH_ABSTAIN")
            self.assertTrue((target / "comparison.html").is_file())
            self.assertFalse(list(Path(folder).rglob("*.sqlite3")))
            manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            for name, expected in manifest["artifact_digests"].items():
                self.assertEqual(digest(json.loads((target / name).read_text(encoding="utf-8"))), expected)

    def test_budget_defers_whole_three_role_cases_and_never_overruns(self):
        with tempfile.TemporaryDirectory() as folder:
            result, _ = run_case(output_root=folder, agents=True, max_total_calls=3,
                search_provider=SearchFixture(), researcher=ReviewFixture(), reviewer=ReviewFixture())
        self.assertEqual(result["model_call_count"], 3)
        self.assertEqual([r["status"] for r in result["results"][:3]],
            ["THREE_AGENT_CASE_COMPLETE", "BUDGET_DEFERRED", "BUDGET_DEFERRED"])

    def test_numeric_case_does_not_call_models_or_load_credentials(self):
        with tempfile.TemporaryDirectory() as folder, patch("support_scout.pattern_case.openai_config", side_effect=AssertionError("key read")):
            result, _ = run_case(output_root=folder)
        self.assertEqual(result["model_call_count"], 0)
        self.assertTrue(all(r["status"] == "NUMERIC_ONLY" for r in result["results"]))

    def test_saved_case_and_three_role_decisions_replay_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            _, target = run_case(output_root=folder, agents=True, search_provider=SearchFixture(),
                researcher=ReviewFixture(), reviewer=ReviewFixture())
            with patch("support_scout.pattern_case.openai_config", side_effect=AssertionError("key read")), \
                    patch("support_scout.providers.post_json", side_effect=AssertionError("network called")):
                replay = replay_case(target)
            self.assertEqual(replay["status"], "MATCH")
            self.assertEqual((replay["network_calls"], replay["model_calls"]), (0, 0))
            path = target / "support_input.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["securities"][0]["bars"][0]["close"] += 100
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(DataError, "ARTIFACT_MISMATCH"):
                replay_case(target)


if __name__ == "__main__":
    unittest.main()
