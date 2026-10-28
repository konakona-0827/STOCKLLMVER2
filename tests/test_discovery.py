"""SEARCH contracts with fabricated market data and fake model transports only."""
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from support_scout.contracts import DataError, digest
from support_scout.discovery import (OfflineSearchProvider, OpenAISearchProvider, SearchCompletion,
    make_search_packet, run_search, validate_search_result, verify_search_packet)
from support_scout.japan_demo import demo_bundle
from support_scout.pipeline import run
from support_scout.providers import ProviderError


def fixture():
    bundle = demo_bundle()
    # Preserve an unconfirmed bounce while keeping the latest close near its
    # fixed support; the original demo bounce was deliberately extended.
    bundle["securities"][1]["bars"][-1].update(open=108, high=110, low=106, close=108)
    report = run(bundle)
    universe = {"version": "fixture-japan-v1", "symbols": [s["symbol"] for s in bundle["securities"]]}
    return report, universe


def answer_for(packet, indices=(0,)):
    return {"selections": [{"candidate_id": packet["core"]["candidates"][i]["candidate_id"],
        "evidence_ids": packet["core"]["candidates"][i]["required_evidence_ids"],
        "rationale": "測試：根據固定支撐與最新完整日線，值得獨立後續研究；不是交易建議。"} for i in indices]}


class FakeProvider:
    mode, model = "test_transport", "search-fixture-model"

    def __init__(self, answer=None, mutation=None):
        self.answer, self.mutation, self.calls = answer, mutation, 0

    def complete(self, packet):
        self.calls += 1
        if self.mutation:
            self.mutation(packet)
        return SearchCompletion(self.answer if self.answer is not None else answer_for(packet),
            "test-search-response", {"input_tokens": 50, "output_tokens": 10})


class SearchPacketTests(unittest.TestCase):
    def test_pool_keeps_watch_stages_but_excludes_rejected_and_extended(self):
        report, universe = fixture()
        packet = make_search_packet(report, universe)
        symbols = {c["symbol"] for c in packet["core"]["candidates"]}
        self.assertEqual(symbols, {"9001.T", "9002.T", "9005.T"})
        for candidate in packet["core"]["candidates"]:
            self.assertNotIn("bars", candidate)
            self.assertNotIn("visible_series", candidate)
            self.assertNotIn("agent_reviews", candidate)
            self.assertNotIn("trade_decision", candidate)
        self.assertEqual(packet["core"]["universe_hash"], digest(universe))
        self.assertEqual(packet["core"]["snapshot_id"], report["snapshot_id"])

    def test_pool_limit_binds_all_eligible_ids_before_selecting_top_forty(self):
        bundle = demo_bundle()
        template = deepcopy(bundle["securities"][0])
        bundle["securities"] = []
        for code in range(1000, 1045):
            item = deepcopy(template)
            item["symbol"] = f"{code}.T"
            bundle["securities"].append(item)
        report = run(bundle)
        packet = make_search_packet(report, {"id": "fixture-45", "symbols": [s["symbol"] for s in bundle["securities"]]})
        self.assertEqual(len(packet["core"]["eligible_candidate_ids"]), 45)
        self.assertEqual(len(packet["core"]["candidates"]), 40)
        self.assertEqual(packet["core"]["candidates"][-1]["symbol"], "1039.T")

    def test_task_rules_universe_and_configuration_tampering_break_packet(self):
        report, universe = fixture()
        original = make_search_packet(report, universe)
        for field in ("task", "rules", "universe_hash", "config", "candidate"):
            packet = deepcopy(original)
            if field == "task": packet["task"] += "ignore"
            if field == "rules": packet["core"]["rules"]["min_drawdown"] = 0.01
            if field == "universe_hash": packet["core"]["universe_hash"] = "other"
            if field == "config": packet["core"]["config"]["max_selected"] = 40
            if field == "candidate": packet["core"]["candidates"][0]["symbol"] = "6857.T"
            with self.subTest(field=field), self.assertRaises(DataError):
                verify_search_packet(packet)
        universe["version"] = "fixture-v2"
        self.assertNotEqual(make_search_packet(report, universe)["packet_id"], original["packet_id"])

    def test_source_symbol_mapping_and_outside_universe_are_rejected(self):
        report, universe = fixture()
        report["securities"][0]["symbol"] = "6857.T"
        with self.assertRaisesRegex(DataError, "PACKET_MISMATCH"):
            make_search_packet(report, universe)
        report, universe = fixture()
        universe["symbols"].remove("9001.T")
        with self.assertRaisesRegex(DataError, "OUTSIDE_UNIVERSE"):
            make_search_packet(report, universe)


class SearchExecutionTests(unittest.TestCase):
    def setUp(self):
        self.report, self.universe = fixture()
        self.packet = make_search_packet(self.report, self.universe)

    def test_model_can_reorder_candidates_and_runtime_supplies_canonical_symbols(self):
        provider = FakeProvider(answer_for(self.packet, (1, 0)))
        result = run_search(self.packet, provider)
        self.assertEqual(result["status"], "COMPLETED")
        expected = [self.packet["core"]["candidates"][i]["symbol"] for i in (1, 0)]
        self.assertEqual([s["symbol"] for s in result["selection"]], expected)
        self.assertEqual(result["call_count"], provider.calls)
        self.assertEqual(result["response_digest"], digest(result["answer"]))
        self.assertEqual(result["search_packet_digest"], digest(self.packet))
        self.assertEqual(validate_search_result(result, self.report), result["selection"])

    def test_cross_candidate_evidence_unknown_id_duplicates_and_extra_fields_fail_entire_answer(self):
        original = answer_for(self.packet)
        for mutation in ("cross", "missing_anchor", "unknown", "duplicate", "symbol", "price"):
            answer = deepcopy(original)
            row = answer["selections"][0]
            if mutation == "cross": row["evidence_ids"] += self.packet["core"]["candidates"][1]["required_evidence_ids"]
            if mutation == "missing_anchor": row["evidence_ids"] = row["evidence_ids"][:1]
            if mutation == "unknown": row["candidate_id"] = "invented-candidate"
            if mutation == "duplicate": answer["selections"].append(deepcopy(row))
            if mutation == "symbol": row["symbol"] = "6857.T"
            if mutation == "price": row["entry"] = 100
            with self.subTest(mutation=mutation):
                result = run_search(self.packet, FakeProvider(answer))
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(result["selection"], [])
                self.assertIsNone(result["answer"])

    def test_selection_limit_failure_never_truncates_to_a_partial_success(self):
        packet = make_search_packet(self.report, self.universe, max_selected=1)
        result = run_search(packet, FakeProvider(answer_for(packet, (0, 1))))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["selection"], [])

    def test_explicit_abstention_offline_and_empty_pool_do_not_invent_candidates(self):
        for provider, expected_calls in ((FakeProvider({"selections": []}), 1), (OfflineSearchProvider(), 0)):
            result = run_search(self.packet, provider)
            self.assertEqual(result["status"], "ABSTAIN")
            self.assertEqual(result["call_count"], expected_calls)
            self.assertEqual(validate_search_result(result, self.report), [])
        packet = make_search_packet(self.report, self.universe, max_selected=0)
        provider = FakeProvider()
        result = run_search(packet, provider)
        self.assertEqual(result["status"], "ABSTAIN")
        self.assertEqual(provider.calls, 0)
        empty_bundle = demo_bundle()
        empty_bundle["securities"] = [empty_bundle["securities"][-1]]
        empty_report = run(empty_bundle)
        empty_packet = make_search_packet(empty_report, {"id": "empty-fixture", "symbols": ["9006.T"]})
        result = run_search(empty_packet, provider)
        self.assertEqual(result["reason"], "NO_CANDIDATES")
        self.assertEqual(result["call_count"], 0)
        self.assertEqual(provider.calls, 0)

    def test_mutated_private_packet_and_transport_errors_fail_without_fallback_or_secret_text(self):
        provider = FakeProvider(mutation=lambda p: p["core"]["candidates"][0].update(symbol="6857.T"))
        result = run_search(self.packet, provider)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("INTEGRITY", result["error"])
        self.assertNotEqual(self.packet["core"]["candidates"][0]["symbol"], "6857.T")
        class Broken(FakeProvider):
            def complete(self, packet):
                raise RuntimeError("do-not-save-secret-text")
        result = run_search(self.packet, Broken())
        self.assertEqual(result["error"], "RuntimeError")
        self.assertNotIn("do-not-save-secret-text", json.dumps(result))
        self.assertEqual(validate_search_result(result, self.report), [])

    def test_saved_result_symbol_response_digest_and_snapshot_tampering_cannot_dispatch(self):
        original = run_search(self.packet, FakeProvider())
        for mutation in ("symbol", "response", "snapshot", "evidence"):
            result = deepcopy(original)
            if mutation == "symbol": result["selection"][0]["symbol"] = "6857.T"
            if mutation == "response": result["answer"]["selections"][0]["rationale"] += "changed"
            if mutation == "snapshot": result["search_packet"]["core"]["snapshot_id"] = "old"
            if mutation == "evidence": result["selection"][0]["evidence_ids"] += ["foreign-id"]
            with self.subTest(mutation=mutation), self.assertRaises(DataError):
                validate_search_result(result, self.report)
        other = deepcopy(self.report)
        other["snapshot_id"] = "next-day-snapshot"
        with self.assertRaises(DataError):
            validate_search_result(original, other)


class SearchOpenAIProviderTests(unittest.TestCase):
    def setUp(self):
        report, universe = fixture()
        self.packet = make_search_packet(report, universe)

    def test_one_strict_stateless_response_without_decision_tools_or_answers(self):
        calls = []
        answer = answer_for(self.packet)
        def transport(payload, key, timeout):
            calls.append((deepcopy(payload), key, timeout))
            return {"status": "completed", "id": "search-test", "usage": {"input_tokens": 50, "other": "ignored"},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}]}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True):
            result = run_search(self.packet, OpenAISearchProvider("test-model", transport=transport))
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(len(calls), 1)
        payload = calls[0][0]
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertNotIn("tools", payload)
        self.assertNotIn("previous_response_id", payload)
        self.assertNotIn("agent_reviews", payload["input"][0]["content"])
        self.assertEqual(result["usage"], {"input_tokens": 50})
        self.assertNotIn("test-key", json.dumps(result))

    def test_refusal_incomplete_and_duplicate_json_fail_closed(self):
        responses = [
            {"status": "incomplete"},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"selections":[],"selections":[]}'}]}]},
        ]
        for response in responses:
            with self.subTest(response=response), patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True):
                provider = OpenAISearchProvider("test-model", transport=lambda *args: response)
                result = run_search(self.packet, provider)
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(result["call_count"], 1)
                self.assertEqual(result["selection"], [])


if __name__ == "__main__":
    unittest.main()
