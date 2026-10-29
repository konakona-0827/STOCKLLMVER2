"""Active manual opportunity gates, using fabricated inputs and fake SEARCH."""
from copy import deepcopy
import unittest

from support_scout.agents import make_packet
from support_scout.contracts import DataError, digest
from support_scout.discovery import SearchCompletion, make_search_packet, run_search
from support_scout.japan_demo import demo_bundle
from support_scout.opportunity import load_policy, active_candidate, attach_opportunities, validate_view
from support_scout.pipeline import run


class SelectingFixture:
    mode, model = "test_transport", "fabricated-search-model"

    def complete(self, packet):
        candidate = packet["core"]["candidates"][0]
        return SearchCompletion({"selections": [{"candidate_id": candidate["candidate_id"],
            "evidence_ids": candidate["required_evidence_ids"],
            "rationale": "人工試單研究用人造支撐，不是實際市場建議。"}]})


def fixture(*, synthetic=False, retrospective=False, stage="SUPPORT_HELD",
            researcher="PROBE_BUY_REVIEW", reviewer="PROBE_BUY_REVIEW", selected=True, core_mutation=None):
    """Create a real canonical packet, while all prices and claims are artificial."""
    bundle = demo_bundle()
    bundle["securities"] = bundle["securities"][:1]
    bundle.update(synthetic=synthetic, observation_only=True,
        collection={"status": "COMPLETE"}, opportunity_policy=load_policy())
    bundle["securities"][0]["observation_only"] = True
    base = run(bundle)
    core = deepcopy(base["all_packets"][0]["core"])
    symbol, as_of = core["symbol"], core["as_of"]
    bars = core["visible_series"]["bars"]
    anchor = bars[-3]["session"]
    for bar in bars[:-3]:
        bar.update(open=110, high=120, low=105, close=110)
    bars[-3].update(open=101, high=103, low=100, close=101)
    bars[-2].update(open=102, high=104, low=101, close=102)
    bars[-1].update(open=102, high=104, low=101, close=102)
    core["technical"].update(state="INVALIDATED", position="EXTENDED",
        latest_close=102, latest_session=bars[-1]["session"], technical_candidate=False)
    # This old ATR episode is intentionally unrelated to the H-002 support.
    core["technical"]["support"].update(anchor=110, lower=108, upper=112)
    refs = [f"{symbol}:H002_ANCHOR:{anchor}", f"{symbol}:H002_HELD:{bars[-1]['session']}"]
    for identifier, kind, bar in zip(refs, ("H002_ANCHOR", "H002_HELD"), (bars[-3], bars[-1])):
        core["evidence"].append({"id": identifier, "kind": kind, "session": bar["session"],
            "available_at": bar["available_at"], "values": {"low": bar["low"], "close": bar["close"]}})
    pattern = {"pattern_id": "H-002", "version": "fabricated-h002-v1", "stage": stage,
        "stage_label": "人造支撐守住", "research_candidate": stage in {"SUPPORT_HELD", "SUPPORT_RECLAIMED"},
        "support": {"anchor_session": anchor, "anchor": 100, "lower": 99, "upper": 101,
            "frozen_at": bars[-2]["session"]},
        "metrics": {"distance_from_support_fraction": 102 / 101 - 1,
            "support_test_sessions": [anchor, bars[-1]["session"]]},
        "definition": {"parameters": {}}, "evidence": deepcopy(core["evidence"][-2:]),
        "required_evidence_ids": refs, "reason_codes": [],
        "empirical_status": "FABRICATED_TEST_NOT_PERFORMANCE"}
    core.update(patterns=[pattern], context_reject=False, context_blockers=["OBSERVATION_ONLY"],
        opportunity_policy=load_policy(), synthetic=synthetic, observation_only=True)
    core["required_confirmation_evidence"] = list(dict.fromkeys([*core["required_confirmation_evidence"], *refs]))
    universe = {"version": "fabricated-active-pool-v1", "symbols": [symbol]}
    core.update(discovery_required=True, source_universe_hash=digest(universe))
    if retrospective:
        core["analysis_context"] = {"mode": "RETROSPECTIVE_REAL_PRICE_CASE_RECONSTRUCTION",
            "market_data_cutoff": as_of, "historical_point_in_time_verified": False}
    if core_mutation:
        core_mutation(core)
    packet = make_packet(core)
    item = deepcopy(base["securities"][0])
    item.update(packet_id=packet["packet_id"], data_status="OBSERVATION", observation_only=True,
        technical={**deepcopy(core["technical"]), "evidence": deepcopy(core["evidence"])},
        patterns=deepcopy(core["patterns"]), context_blockers=deepcopy(core["context_blockers"]),
        verdict="REJECT", reasons=["HARD_INVALIDATION"], agent_errors=[])
    reviews = {}
    for role, action in (("researcher", researcher), ("reviewer", reviewer)):
        if action is not None:
            reviews[role] = {"packet_id": packet["packet_id"], "role": role,
                "agent_id": "fabricated-" + role, "produced_at": as_of,
                "verdict": "WATCH", "evidence_ids": core["required_confirmation_evidence"],
                "summary": "人造案例：價量支持條件式人工試單評估，但來源未獨立核實。",
                "risks": ["人造資料不代表真實投資機會"], "missing_data": ["資料源仍待核實"],
                "opportunity_view": {"action": action, "pattern_id": "H-002", "reason": "人造支撐與風報條件供測試。"}}
    item["agent_reviews"] = reviews
    report = {**deepcopy(base), "securities": [item], "agent_packets": [packet], "all_packets": [packet],
        "snapshot_id": digest([packet["packet_id"]]), "discovery_required": True,
        "source_universe_hash": digest(universe), "reviews": list(reviews.values()),
        "opportunity_policy": load_policy(), "unmatched_review_packets": []}
    report["discovery"] = {"mode": "AGENT_SEARCH", "search": None}
    if selected:
        search_packet = make_search_packet(report, universe, max_selected=1)
        report["discovery"]["search"] = run_search(search_packet, SelectingFixture())
    bundle.update(source_universe=universe, discovery_required=True)
    if retrospective:
        bundle["analysis_context"] = deepcopy(core["analysis_context"])
    return report, bundle, core


class OpportunityTests(unittest.TestCase):
    def test_policy_is_versioned_strict_and_rejects_invalid_numeric_values(self):
        policy = load_policy()
        self.assertEqual(policy["mode"], "ACTIVE_MANUAL")
        for invalid in ([], {**policy, "unexpected": True}, {**policy, "minimum_net_rr": -1},
                        {**policy, "max_risk_fraction": float("nan")}, {**policy, "enabled": 1}):
            with self.subTest(invalid=invalid), self.assertRaises(DataError):
                load_policy(invalid)

    def test_view_requires_real_pattern_and_its_support_evidence(self):
        core = {"opportunity_policy": load_policy(), "technical": {"state": "INVALIDATED"},
            "context_reject": False, "patterns": [{"pattern_id": "H-002", "research_candidate": True,
                "stage": "SUPPORT_HELD", "required_evidence_ids": ["anchor", "held"]}]}
        view = {"action": "PROBE_BUY_REVIEW", "pattern_id": "H-002", "reason": "人造支撐證據"}
        validate_view(view, core, ["anchor", "held"])
        with self.assertRaises(DataError): validate_view(view, core, ["anchor"])
        with self.assertRaises(DataError): validate_view({**view, "pattern_id": "H-999"}, core, ["anchor", "held"])
        core["context_reject"] = True
        self.assertFalse(active_candidate(core))
        with self.assertRaises(DataError): validate_view(view, core, ["anchor", "held"])

    def test_h002_admission_is_independent_of_old_atr_invalidation_and_distance(self):
        report, _, core = fixture()
        self.assertTrue(active_candidate(core))
        self.assertEqual(report["discovery"]["search"]["status"], "COMPLETED")
        self.assertEqual(len(report["discovery"]["search"]["selection"]), 1)
        for stage in ("INVALIDATED", "EXTENDED", "NO_MATCH"):
            changed = deepcopy(core)
            changed["patterns"][0].update(stage=stage, research_candidate=False)
            with self.subTest(stage=stage): self.assertFalse(active_candidate(changed))

    def test_two_positive_agents_with_search_and_h002_can_propose_manual_probe(self):
        report, bundle, _ = fixture()
        formal = deepcopy(report["securities"][0]["trade_decision"])
        attach_opportunities(report, bundle)
        result = report["securities"][0]["opportunity_decision"]
        self.assertEqual(result["action"], "PROBE_BUY_REVIEW")
        self.assertFalse(result["execution_enabled"])
        self.assertEqual(report["securities"][0]["trade_decision"], formal)

    def test_reviewer_veto_and_missing_review_cannot_be_promoted(self):
        for reviewer, expected in (("AVOID", "AVOID"), ("WAIT", "WAIT"), (None, "WAIT")):
            report, bundle, _ = fixture(reviewer=reviewer)
            attach_opportunities(report, bundle)
            with self.subTest(reviewer=reviewer):
                self.assertEqual(report["securities"][0]["opportunity_decision"]["action"], expected)

    def test_missing_search_submission_cannot_be_promoted(self):
        report, bundle, _ = fixture(selected=False)
        attach_opportunities(report, bundle)
        self.assertNotEqual(report["securities"][0]["opportunity_decision"]["action"], "PROBE_BUY_REVIEW")

    def test_low_reward_risk_and_excessive_distance_prevent_probe(self):
        def weak_resistance(core):
            anchor = core["patterns"][0]["support"]["anchor_session"]
            for bar in core["visible_series"]["bars"]:
                if bar["session"] < anchor: bar.update(open=102, high=103, low=101, close=102)
        def extended(core):
            core["patterns"][0]["metrics"]["distance_from_support_fraction"] = 0.20
        for change in (weak_resistance, extended):
            report, bundle, _ = fixture(core_mutation=change)
            attach_opportunities(report, bundle)
            with self.subTest(change=change.__name__):
                self.assertEqual(report["securities"][0]["opportunity_decision"]["action"], "AVOID")

    def test_nearer_confirmed_resistance_cannot_be_skipped_for_a_distant_high(self):
        def nearer_resistance(core):
            anchor = core["patterns"][0]["support"]["anchor_session"]
            previous = [b for b in core["visible_series"]["bars"] if b["session"] < anchor]
            # A visible local high at106 is already confirmed by lower highs
            # on both sides; the much older120 remains in the20-session window.
            for bar, high in zip(previous[-6:], (105, 106, 105, 104, 103, 102)):
                bar.update(open=high-1, high=high, low=high-2, close=high-1)
        report, bundle, _ = fixture(core_mutation=nearer_resistance)
        attach_opportunities(report, bundle)
        result = report["securities"][0]["opportunity_decision"]
        self.assertNotEqual(result["action"], "PROBE_BUY_REVIEW")
        self.assertEqual(result["reference_plan"]["target_reference"], 106)

    def test_source_revision_and_stale_ledger_cannot_be_active_buy_signals(self):
        for change in ("STALE_IGNORED", "REVISION_REVIEW"):
            report, bundle, _ = fixture()
            report["ledger"] = {"status": "COMPLETE", "errors": [], "rows": [
                {"symbol": report["securities"][0]["symbol"], "change": change,
                 "revision_review": change == "REVISION_REVIEW", "current_state": "INVALIDATED"}]}
            attach_opportunities(report, bundle)
            with self.subTest(change=change):
                self.assertNotEqual(report["securities"][0]["opportunity_decision"]["action"], "PROBE_BUY_REVIEW")

    def test_old_atr_ledger_invalidation_alone_does_not_veto_independent_h002(self):
        report, bundle, _ = fixture()
        report["ledger"] = {"status": "COMPLETE", "errors": [], "rows": [
            {"symbol": report["securities"][0]["symbol"], "change": "UNCHANGED",
             "revision_review": False, "current_state": "INVALIDATED"}]}
        attach_opportunities(report, bundle)
        self.assertEqual(report["securities"][0]["opportunity_decision"]["action"], "PROBE_BUY_REVIEW")

    def test_changed_policy_and_source_universe_need_a_new_bound_search(self):
        for mutation in ("policy", "source_universe"):
            report, bundle, _ = fixture()
            if mutation == "policy":
                report["opportunity_policy"]["max_risk_fraction"] = 0.20
                bundle["opportunity_policy"]["max_risk_fraction"] = 0.20
            else:
                bundle["source_universe"] = {"version": "different-pool", "symbols": ["9002.T"]}
            attach_opportunities(report, bundle)
            with self.subTest(mutation=mutation):
                self.assertNotEqual(report["securities"][0]["opportunity_decision"]["action"], "PROBE_BUY_REVIEW")

    def test_recalculation_replaces_forged_derived_positive_after_veto(self):
        report, bundle, _ = fixture(reviewer="AVOID")
        item = report["securities"][0]
        item["opportunity_decision"] = {"action": "PROBE_BUY_REVIEW", "scope": "CONDITIONAL_MANUAL_REVIEW"}
        attach_opportunities(report, bundle)
        self.assertEqual(item["opportunity_decision"]["action"], "AVOID")

    def test_unknown_review_evidence_and_packet_tampering_never_promote(self):
        for mutation in ("review_evidence", "report_pattern"):
            report, bundle, _ = fixture()
            item = report["securities"][0]
            if mutation == "review_evidence": item["agent_reviews"]["reviewer"]["evidence_ids"] = ["fabricated-other-stock"]
            else: item["patterns"][0]["support"]["lower"] = 80
            attach_opportunities(report, bundle)
            with self.subTest(mutation=mutation):
                self.assertNotEqual(item["opportunity_decision"]["action"], "PROBE_BUY_REVIEW")

    def test_synthetic_and_historical_probe_are_explicitly_simulation_only(self):
        for flags in ({"synthetic": True}, {"retrospective": True}):
            report, bundle, _ = fixture(**flags)
            attach_opportunities(report, bundle)
            result = report["securities"][0]["opportunity_decision"]
            with self.subTest(flags=flags):
                self.assertEqual(result["action"], "PROBE_BUY_REVIEW")
                self.assertIn("SIMULATION_ONLY", str(result))
                self.assertFalse(result["execution_enabled"])


if __name__ == "__main__":
    unittest.main()
