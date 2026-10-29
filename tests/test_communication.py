"""Compact packets retain decision evidence, while replies stay request-bound."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from support_scout.agents import make_packet
from support_scout.communication import (bounded_schema, encode, expand_refs, expand_search,
    load_policy, review_wire, search_wire, validate_wire_answer, wire_metrics)
from support_scout.contracts import DataError
from support_scout.discovery import make_search_packet, OpenAISearchProvider
from support_scout.pattern_case import build_case_bundle, load_case
from support_scout.pipeline import run
from support_scout.providers import ANSWER_SCHEMA, OpenAIProvider


def fixture(checkpoint="support"):
    bundle = build_case_bundle(load_case(), checkpoint)
    bundle["communication_policy"] = load_policy()
    report = run(bundle)
    return bundle, report, report["agent_packets"][0]


def answer(refs):
    return {"verdict": "WATCH", "evidence_ids": refs, "summary": "合成測試；仍有資料缺口。",
        "risks": ["支撐未經樣本外驗證"], "missing_data": ["完整公司資訊"]}


class CommunicationTests(unittest.TestCase):
    def test_policy_has_versioned_finite_integer_limits(self):
        for patch in ({"version": "unknown"}, {"recent_bars": True}, {"max_input_bytes": 3999},
                {"public_source_mode": "silent_peer_chat"}, {"extra": 1}):
            with self.subTest(patch=patch), self.assertRaises(DataError):
                load_policy({**load_policy(), **patch})

    def test_review_preserves_cutoff_context_parameters_and_evidence_values(self):
        _, _, packet = fixture("catalyst")
        view, aliases, _ = review_wire(packet)
        self.assertEqual(view["analysis_context"], packet["core"]["analysis_context"])
        self.assertEqual(view["as_of"], packet["core"]["as_of"])
        self.assertEqual(view["technical_parameters"], packet["core"]["rules"])
        original = {e["id"]: e for e in packet["core"]["evidence"]}
        for short, identifier in aliases.items():
            self.assertEqual(view["evidence"][short]["value"], original[identifier].get("values"))
            self.assertEqual(view["evidence"][short]["at"], original[identifier]["available_at"])
        self.assertEqual(view["pattern_parameters"]["H-001"], packet["core"]["patterns"][0]["definition"]["parameters"])

    def test_recent_and_old_counterevidence_bars_are_both_retained(self):
        _, _, original = fixture()
        core = deepcopy(original["core"])
        first = core["visible_series"]["bars"][0]
        core["evidence"].append({"id": core["symbol"]+":OLD_CONTROL", "kind": "INVALIDATED",
            "session": first["session"], "available_at": first["available_at"],
            "values": {"close": first["close"], "frozen_lower": first["close"]+1}})
        packet = make_packet(core)
        view, aliases, _ = review_wire(packet)
        dates = [r[0] for r in view["bars"]["rows"]]
        self.assertIn(first["session"], dates)
        self.assertIn(core["visible_series"]["bars"][-1]["session"], dates)
        self.assertGreater(view["bars"]["omitted_older_bars"], 0)
        control = next(k for k, v in aliases.items() if v.endswith(":OLD_CONTROL"))
        self.assertEqual(view["evidence"][control]["value"]["frozen_lower"], first["close"]+1)

    def test_exact_prices_and_bar_availability_survive_json_round_trip(self):
        _, _, original = fixture()
        core = deepcopy(original["core"])
        last = core["visible_series"]["bars"][-1]
        last["close"] += 0.123456789
        packet = make_packet(core)
        view, _, _ = review_wire(packet)
        decoded = json.loads(encode(view))
        self.assertEqual(decoded["bars"]["rows"][-1][4], last["close"])
        self.assertEqual(decoded["bars"]["rows"][-1][-2:], [last["closed_at"], last["available_at"]])

    def test_position_and_historical_analysis_context_are_never_inferred_or_cut(self):
        _, _, original = fixture()
        core = deepcopy(original["core"])
        position = {"quantity": 100, "average_cost": 30123.45678, "opened_at": "2026-10-23T10:03:00+09:00",
            "source": "USER_CONFIRMED_TEST_FILL"}
        core["portfolio_position"] = position
        core["analysis_context"]["diagnostic_limitations"] = ["今天下載歷史資料不是當時可得快照", "不准引用未公布催化"]
        view, _, _ = review_wire(make_packet(core))
        self.assertEqual(view["portfolio_position"], position)
        self.assertEqual(view["analysis_context"], core["analysis_context"])

    def test_reply_aliases_are_bound_to_packet_symbol_and_integrity(self):
        _, _, packet = fixture()
        _, aliases, _ = review_wire(packet)
        short = next(iter(aliases))
        self.assertEqual(expand_refs(answer([short]), aliases, packet_id=packet["packet_id"],
            symbol=packet["core"]["symbol"])["evidence_ids"], [aliases[short]])
        for kwargs in ({"packet_id": "other"}, {"symbol": "9999.T"}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(DataError, "CONTEXT"):
                expand_refs(answer([short]), aliases, **kwargs)
        aliases[short] = "9999.T:MADE_UP"
        with self.assertRaisesRegex(DataError, "CONTEXT"):
            expand_refs(answer([short]), aliases)

    def test_unknown_duplicate_and_cross_symbol_evidence_are_rejected(self):
        _, _, packet = fixture()
        _, aliases, _ = review_wire(packet)
        short = next(iter(aliases))
        for refs in (["missing"], [short, short], [123], ["9999.T:LATEST"]):
            with self.subTest(refs=refs), self.assertRaises(DataError):
                expand_refs(answer(refs), aliases)
        core = deepcopy(packet["core"])
        core["evidence"][0]["id"] = "9999.T:ANCHOR:2026-10-21"
        with self.assertRaisesRegex(DataError, "FOREIGN"):
            review_wire(make_packet(core))

    def test_tampered_original_packet_is_not_exported(self):
        _, _, packet = fixture()
        packet["core"]["visible_series"]["bars"][-1]["close"] += 100
        with self.assertRaisesRegex(DataError, "INTEGRITY"):
            review_wire(packet)

    def test_search_keeps_parameters_and_dictionaries_resolve_to_actual_values(self):
        bundle, report, _ = fixture()
        packet = make_search_packet(report, bundle["source_universe"], max_selected=1)
        view, mapping, _ = search_wire(packet)
        c = view["candidates"][0]
        technical = dict(zip(view["layout"]["technical_values"], c["technical_values"]))
        original = packet["core"]["candidates"][0]
        self.assertEqual(technical["latest_close"], original["technical"]["latest_close"])
        self.assertEqual([view["codes"][x] for x in c["blockers"]], original["context_blockers"])
        self.assertEqual(view["pattern_parameters"]["H-001"]["parameters"], original["patterns"][0]["definition"]["parameters"])
        self.assertEqual(c["analysis_context"], original["analysis_context"])
        self.assertEqual(view["technical_parameters"], packet["core"]["rules"])
        identifier, refs = mapping[c["id"]]
        expanded = expand_search({"selections": [{"candidate_id": c["id"], "evidence_ids": c["required"],
            "rationale": "合成測試"}]}, mapping, packet_id=packet["packet_id"])
        self.assertEqual(expanded["selections"][0]["candidate_id"], identifier)
        self.assertEqual(expanded["selections"][0]["evidence_ids"], [refs[e] for e in c["required"]])

    def test_search_unknown_duplicate_or_foreign_alias_does_not_silently_expand(self):
        bundle, report, _ = fixture()
        packet = make_search_packet(report, bundle["source_universe"], max_selected=1)
        view, mapping, _ = search_wire(packet)
        c = view["candidates"][0]
        valid = {"candidate_id": c["id"], "evidence_ids": c["required"], "rationale": "合成測試"}
        for rows in ([{**valid, "candidate_id": "c999"}], [valid, valid], [{**valid,"evidence_ids":["e999"]}]):
            with self.subTest(rows=rows), self.assertRaises(DataError):
                expand_search({"selections": rows}, mapping, packet_id=packet["packet_id"])
        with self.assertRaisesRegex(DataError, "CONTEXT"):
            expand_search({"selections":[valid]}, mapping, packet_id="another-search")

    def test_search_preserves_catalyst_announcement_and_pre_event_expectation(self):
        bundle, report, _ = fixture("catalyst")
        packet = make_search_packet(report,bundle["source_universe"],max_selected=1)
        view, mapping, _ = search_wire(packet)
        facts = view["candidates"][0]["event_facts"]
        _, aliases = mapping[view["candidates"][0]["id"]]
        original = {e["id"]:e for e in packet["core"]["candidates"][0]["evidence"]}
        key = next(k for k in facts if original[aliases[k]]["kind"]=="H001_CATALYST" and facts[k].get("comparison"))
        self.assertEqual(facts[key],original[aliases[key]]["values"])
        self.assertIn("announced_at",facts[key])
        self.assertIn("expectation_available_at",facts[key]["comparison"])

    def test_summary_and_list_limits_are_local_checks_as_well_as_schema(self):
        policy = load_policy()
        schema = bounded_schema(ANSWER_SCHEMA, policy)
        self.assertEqual(schema["properties"]["summary"]["maxLength"], policy["summary_chars"])
        self.assertNotIn("maxLength", ANSWER_SCHEMA["properties"]["summary"])
        validate_wire_answer(answer(["e1"]), policy)
        for field, value in (("summary", "x"*(policy["summary_chars"]+1)),
                ("risks", ["x"]*(policy["list_items"]+1)), ("missing_data", ["x"*(policy["item_chars"]+1)])):
            with self.subTest(field=field), self.assertRaises(DataError):
                validate_wire_answer({**answer(["e1"]),field:value}, policy)
        with self.assertRaises(DataError):
            validate_wire_answer({"selections":[{"rationale":"x"*(policy["item_chars"]+1)}]}, policy, search=True)

    def test_byte_savings_are_measured_without_claiming_billed_token_savings(self):
        _, _, packet = fixture()
        _, _, metrics = review_wire(packet)
        self.assertGreater(metrics["byte_reduction_fraction"], 0.6)
        self.assertEqual(metrics["measure"], "UTF8_BYTES_NOT_BILLED_TOKENS")
        self.assertIsNone(metrics["estimated_tokens"]["wire"])
        self.assertFalse(metrics["estimated_tokens"]["billed_usage"])
        counted = wire_metrics({"old": "abcdef"}, {"x": "a"}, load_policy(), token_counter=len)
        self.assertEqual(counted["estimated_tokens"]["method"], "INJECTED_TOKENIZER_ESTIMATE")
        self.assertLess(counted["estimated_tokens"]["wire"], counted["estimated_tokens"]["original"])

    def test_appending_shared_public_facts_must_still_fit_the_same_budget(self):
        _, _, packet = fixture()
        view, _, _ = review_wire(packet)
        view["public_sources"] = {"findings": [{"fact": "x"*30000}]}
        with self.assertRaisesRegex(DataError, "BUDGET"):
            wire_metrics(packet["core"], view, load_policy())

    def test_search_budget_is_independent_and_old_policy_is_not_rewritten(self):
        old = load_policy()
        old.pop("search_max_input_bytes", None)
        self.assertNotIn("search_max_input_bytes", load_policy(old))
        policy = {**old, "max_input_bytes": 4000, "search_max_input_bytes": 6000}
        payload = {"data": "x"*5000}
        with self.assertRaisesRegex(DataError, "BUDGET"):
            wire_metrics(payload, payload, policy)
        metrics = wire_metrics(payload, payload, policy, role="search")
        self.assertEqual(metrics["input_budget_bytes"], 6000)
        self.assertEqual(metrics["role"], "search")
        with self.assertRaisesRegex(DataError, "BUDGET"):
            wire_metrics(payload, payload, {**old,"max_input_bytes":4000}, role="search")
        for value in (True, 999, 100001):
            with self.subTest(value=value), self.assertRaises(DataError):
                load_policy({**old, "search_max_input_bytes": value})

    def test_role_metrics_do_not_accept_unrecognized_budget_scope(self):
        with self.assertRaisesRegex(DataError, "ROLE"):
            wire_metrics({}, {}, load_policy(), role="anything")

    def test_review_provider_fake_transport_receives_compact_context_and_restores_refs(self):
        _, _, packet = fixture()
        requests = []
        def transport(payload, key, timeout):
            requests.append(payload)
            view = json.loads(payload["input"][0]["content"])
            body = answer(view["required"])
            return {"status":"completed", "id":"fixture-response", "usage":{"input_tokens":100,"output_tokens":20},
                "output":[{"type":"message","content":[{"type":"output_text","text":json.dumps(body)}]}]}
        with patch.dict("os.environ", {"OPENAI_API_KEY":"fixture-not-a-real-key"}):
            completion = OpenAIProvider("fixture-model",transport=transport).complete(packet,"researcher")
        self.assertEqual(completion.answer["evidence_ids"], packet["core"]["required_confirmation_evidence"])
        self.assertEqual(completion.usage["input_tokens"], 100)
        view = json.loads(requests[0]["input"][0]["content"])
        self.assertEqual(view["analysis_context"], packet["core"]["analysis_context"])
        self.assertNotIn("visible_series",view)
        self.assertNotIn("tools",requests[0])

    def test_search_provider_fake_transport_restores_candidate_and_own_refs(self):
        bundle, report, _ = fixture()
        packet = make_search_packet(report,bundle["source_universe"],max_selected=1)
        def transport(payload,key,timeout):
            view = json.loads(payload["input"][0]["content"])
            candidate = view["candidates"][0]
            body = {"selections":[{"candidate_id":candidate["id"],"evidence_ids":candidate["required"],"rationale":"僅供測試"}]}
            return {"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":json.dumps(body)}]}]}
        with patch.dict("os.environ", {"OPENAI_API_KEY":"fixture-not-a-real-key"}):
            completion = OpenAISearchProvider("fixture-model",transport=transport).complete(packet)
        candidate = packet["core"]["candidates"][0]
        self.assertEqual(completion.answer["selections"][0]["candidate_id"],candidate["candidate_id"])
        self.assertEqual(completion.answer["selections"][0]["evidence_ids"],candidate["required_evidence_ids"])


if __name__ == "__main__":
    unittest.main()
