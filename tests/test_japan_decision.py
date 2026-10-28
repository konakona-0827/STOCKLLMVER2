"""Decision boundary tests; all market and review inputs here are fabricated."""
from copy import deepcopy
from dataclasses import replace
import unittest

from support_scout.contracts import DataError
from support_scout.decision import DecisionPolicy, attach_decisions, render_decisions_markdown
from support_scout.japan_demo import demo_bundle
from support_scout.pipeline import run


def complete_fixture():
    bundle = demo_bundle()
    # A test stand-in for a future fully verified feed, never a real stock claim.
    bundle["synthetic"] = False
    bundle["securities"] = bundle["securities"][:1]
    bundle["collection"] = {"status": "COMPLETE"}
    return bundle


def reviews_for(packet, *, reviewer="RESEARCH"):
    return [{"packet_id": packet["packet_id"], "role": role, "agent_id": "fixture-" + role,
        "verdict": reviewer if role == "reviewer" else "RESEARCH",
        "evidence_ids": packet["core"]["required_confirmation_evidence"],
        "summary": "僅供測試：固定支撐及反證均完成。", "risks": [], "missing_data": [],
        "produced_at": packet["core"]["as_of"]} for role in ("researcher", "reviewer")]


def evaluate(bundle=None, *, policy=None, reviewer="RESEARCH", reviews=True):
    bundle = complete_fixture() if bundle is None else bundle
    initial = run(bundle)
    packet = next((p for p in initial["agent_packets"] if p["core"]["symbol"] == bundle["securities"][0]["symbol"]), None)
    report = run(bundle, reviews=reviews_for(packet, reviewer=reviewer) if reviews and packet else None)
    attach_decisions(report, bundle, policy=policy or DecisionPolicy(minimum_net_rr=1.0))
    return report, bundle


def decision(report):
    return report["securities"][0]["trade_decision"]


class JapanDecisionTests(unittest.TestCase):
    def test_complete_verified_case_can_be_considered_without_execution(self):
        report, _ = evaluate()
        result = decision(report)
        self.assertEqual(result["conclusion"], "CONSIDER")
        self.assertFalse(result["execution_enabled"])
        self.assertEqual(report["decision_summary"]["consider_symbols"], ["9001.T"])
        self.assertIn("NEXT_SESSION", result["reference_plan"]["validity"])
        self.assertEqual(result["account_loss_guard"], "NOT_AVAILABLE_NO_ACCOUNT_OR_POSITION_DATA")

    def test_target_is_visible_pre_anchor_resistance_not_manufactured_rr(self):
        report, bundle = evaluate()
        plan = decision(report)["reference_plan"]
        anchor = report["securities"][0]["technical"]["support"]["anchor_at"]
        previous = [b for b in bundle["securities"][0]["bars"] if b["session"] < anchor][-20:]
        self.assertEqual(plan["target_reference"], max(b["high"] for b in previous))
        self.assertLess(plan["target_evidence"]["window_end"], anchor)
        self.assertNotAlmostEqual(plan["target_reference"], plan["entry_reference"] + 2 * (plan["entry_reference"] - plan["invalidation_reference"]))
        self.assertEqual(plan["currency"], "JPY")
        self.assertIn("不保證", plan["invalidation_note"])

    def test_synthetic_even_with_complete_dual_reviews_never_consider(self):
        bundle = complete_fixture()
        bundle["synthetic"] = True
        report, _ = evaluate(bundle)
        self.assertEqual(decision(report)["conclusion"], "WAIT")
        self.assertIn("SYNTHETIC_DATA", decision(report)["reason_codes"])

    def test_raw_yahoo_observation_cannot_be_promoted_by_two_approvers(self):
        bundle = complete_fixture()
        bundle["observation_only"] = True
        bundle["securities"][0].update(observation_only=True, price_basis="raw_unverified", actions_reviewed=False,
            tradability_reviewed=False, volume_comparable=False, point_in_time=False)
        report, _ = evaluate(bundle)
        self.assertEqual(decision(report)["conclusion"], "WAIT")
        self.assertIn("OBSERVATION_ONLY", decision(report)["reason_codes"])
        self.assertTrue(decision(report)["next_steps"])

    def test_reviewer_veto_and_invalidated_support_are_no_trade(self):
        report, _ = evaluate(reviewer="REJECT")
        self.assertEqual(decision(report)["conclusion"], "NO_TRADE")
        bundle = demo_bundle()
        bundle["synthetic"] = False
        bundle["securities"] = [bundle["securities"][2]]
        report, _ = evaluate(bundle)
        self.assertEqual(decision(report)["conclusion"], "NO_TRADE")
        self.assertIn("SUPPORT_INVALIDATED", decision(report)["reason_codes"])
        self.assertNotIn("AGENT_VALIDATION_FAILED", decision(report)["reason_codes"])
        self.assertNotIn("SNAPSHOT_INTEGRITY_FAILED", decision(report)["reason_codes"])
        self.assertNotIn("DUAL_REVIEW_REQUIRED", decision(report)["reason_codes"])

    def test_technical_confirmation_alone_does_not_replace_agents_or_company_review(self):
        report, _ = evaluate(reviews=False)
        self.assertEqual(decision(report)["conclusion"], "WAIT")
        self.assertIn("DUAL_REVIEW_REQUIRED", decision(report)["reason_codes"])
        bundle = complete_fixture()
        bundle["securities"][0]["dossier"] = {}
        report, _ = evaluate(bundle)
        self.assertEqual(decision(report)["conclusion"], "WAIT")
        self.assertIn("CONTEXT_GATE_BLOCKED", decision(report)["reason_codes"])

    def test_price_far_from_frozen_support_is_no_trade(self):
        bundle = demo_bundle()
        bundle["synthetic"] = False
        bundle["securities"] = [bundle["securities"][3]]
        report, _ = evaluate(bundle)
        self.assertEqual(decision(report)["conclusion"], "NO_TRADE")
        self.assertIn("NOT_NEAR_SUPPORT", decision(report)["reason_codes"])

    def test_cost_and_gap_assumptions_can_remove_an_apparently_attractive_setup(self):
        report, bundle = evaluate(policy=DecisionPolicy(minimum_net_rr=1, roundtrip_cost_bps=0, gap_buffer_atr=0))
        self.assertEqual(decision(report)["conclusion"], "CONSIDER")
        gross = decision(report)["reference_plan"]["gross_reward_risk"]
        attach_decisions(report, bundle, policy=DecisionPolicy(minimum_net_rr=1, roundtrip_cost_bps=500, gap_buffer_atr=2))
        result = decision(report)
        self.assertEqual(result["conclusion"], "NO_TRADE")
        self.assertIn("NET_RR_BELOW_MINIMUM", result["reason_codes"])
        self.assertEqual(result["reference_plan"]["gross_reward_risk"], gross)
        self.assertLess(result["reference_plan"]["net_reward_risk"], gross)

    def test_ledger_revision_stale_error_and_prior_invalidation_downgrade(self):
        cases = [({"rows": [{"symbol": "9001.T", "change": "REVISION_REVIEW"}]}, "LEDGER_REVISION_REVIEW", "WAIT"),
            ({"rows": [{"symbol": "9001.T", "change": "STALE_IGNORED"}]}, "LEDGER_STALE", "WAIT"),
            ({"errors": [{"symbol": "other", "error": "write_failed"}]}, "LEDGER_ERROR", "WAIT"),
            ({"rows": [{"symbol": "9001.T", "current_state": "INVALIDATED"}]}, "LEDGER_INVALIDATED", "NO_TRADE")]
        for ledger, code, conclusion in cases:
            with self.subTest(code=code):
                report, bundle = evaluate()
                report["ledger"] = ledger
                attach_decisions(report, bundle, policy=DecisionPolicy(minimum_net_rr=1))
                self.assertIn(code, decision(report)["reason_codes"])
                self.assertEqual(decision(report)["conclusion"], conclusion)

    def test_packet_tampering_and_unmatched_reviews_cannot_preserve_consider(self):
        report, bundle = evaluate()
        report["securities"][0]["technical"]["latest_close"] += 1
        attach_decisions(report, bundle, policy=DecisionPolicy(minimum_net_rr=1))
        self.assertIn("SNAPSHOT_INTEGRITY_FAILED", decision(report)["reason_codes"])
        self.assertNotEqual(decision(report)["conclusion"], "CONSIDER")
        report, bundle = evaluate()
        report["unmatched_review_packets"] = ["earlier-packet"]
        attach_decisions(report, bundle, policy=DecisionPolicy(minimum_net_rr=1))
        self.assertEqual(decision(report)["conclusion"], "WAIT")

    def test_mutating_rejected_company_verdict_cannot_override_hashed_hard_gate(self):
        bundle = complete_fixture()
        bundle["securities"][0]["dossier"]["thesis_status"] = "BROKEN"
        report, bundle = evaluate(bundle)
        self.assertEqual(report["securities"][0]["verdict"], "REJECT")
        self.assertTrue(report["agent_packets"][0]["core"]["context_reject"])
        report["securities"][0].update(verdict="RESEARCH", reasons=[])
        attach_decisions(report, bundle, policy=DecisionPolicy(minimum_net_rr=1))
        self.assertEqual(decision(report)["conclusion"], "NO_TRADE")
        self.assertIn("RESEARCH_REJECTED", decision(report)["reason_codes"])
        self.assertIn("SNAPSHOT_INTEGRITY_FAILED", decision(report)["reason_codes"])

    def test_report_flags_cannot_remove_packet_or_bundle_observation_restrictions(self):
        for mutation in ("report_only", "report_and_item", "bundle"):
            with self.subTest(mutation=mutation):
                bundle = complete_fixture()
                bundle["observation_only"] = True
                bundle["securities"][0].update(observation_only=True, price_basis="raw_unverified", actions_reviewed=False,
                    tradability_reviewed=False, volume_comparable=False, point_in_time=False)
                report, bundle = evaluate(bundle)
                item = report["securities"][0]
                if mutation == "bundle":
                    bundle["observation_only"] = False
                else:
                    report["observation_only"] = False
                    if mutation == "report_and_item":
                        item.update(observation_only=False, data_status="VALID", verdict="RESEARCH", reasons=[], context_blockers=[])
                attach_decisions(report, bundle, policy=DecisionPolicy(minimum_net_rr=1))
                self.assertNotEqual(decision(report)["conclusion"], "CONSIDER")
                self.assertIn("SNAPSHOT_INTEGRITY_FAILED", decision(report)["reason_codes"])

    def test_future_price_spike_cannot_improve_target_or_rr(self):
        report, bundle = evaluate()
        before = deepcopy(decision(report)["reference_plan"])
        future = deepcopy(bundle["securities"][0]["bars"][-1])
        future.update(session="2026-09-09", closed_at="2026-09-09T15:30:00+09:00",
            available_at="2026-09-09T16:00:00+09:00", high=99999)
        bundle["securities"][0]["bars"].append(future)
        report, _ = evaluate(bundle)
        self.assertEqual(decision(report)["reference_plan"], before)

    def test_repeated_attach_is_idempotent_and_markdown_exposes_limits(self):
        report, bundle = evaluate()
        original = deepcopy(report)
        attach_decisions(report, bundle, policy=DecisionPolicy(minimum_net_rr=1))
        self.assertEqual(report, original)
        markdown = render_decisions_markdown(report)
        self.assertIn("可考慮交易", markdown)
        self.assertIn("固定失效價", markdown)
        self.assertIn("下一交易時段", markdown)
        self.assertIn("券商實際費率尚未核實", markdown)

    def test_policy_rejects_nonfinite_or_silent_boolean_configuration(self):
        for override in ({"minimum_net_rr": float("nan")}, {"gap_buffer_atr": True},
                         {"roundtrip_cost_bps": -1}, {"resistance_lookback_sessions": False}):
            with self.subTest(override=override), self.assertRaises(DataError):
                replace(DecisionPolicy(), **override)


if __name__ == "__main__":
    unittest.main()
