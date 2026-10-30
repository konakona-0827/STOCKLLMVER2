from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import patch

from support_scout.agents import make_packet, validate_reviews
from support_scout.contracts import DataError
from support_scout.desk import _call
from support_scout.japan_demo import demo_bundle
from support_scout.pipeline import run
from support_scout.providers import OpenAIProvider, ProviderError
from support_scout.public_information import (load_policy, build_public_information,
    verify_public_information, validate_live_cutoff)
from support_scout.reporting import render_html


class PublicInformationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.core = {"as_of": self.now.isoformat(), "market": "JP", "synthetic": False,
            "symbol": "6857.T", "evidence": [{"id": "fixture", "kind": "FIXTURE"}],
            "required_confirmation_evidence": ["fixture"], "public_information_policy": {**load_policy(), "max_tool_calls_per_review": 3},
            "visible_series": {"name": "Advantest"}}
        self.packet = make_packet(self.core)
        self.finding = {"category": "EARNINGS", "source_type": "COMPANY_IR", "title": "測試用決算資料",
            "url": "https://issuer.example.test/ir/results", "published_at": (self.now - timedelta(days=1)).isoformat(),
            "time_precision": "EXACT", "fact": "合成測試抽取，非真實財務數字。", "period": "TEST",
            "expectation_basis": None, "implication": "NEUTRAL"}
        self.calls = [{"id": "web_test", "status": "completed", "action": {"type": "search", "query": "test",
            "sources": [{"url": self.finding["url"], "title": "fixture"}]}}]

    def info(self, finding=None, calls=None):
        return build_public_information(self.core, [finding or self.finding], calls or self.calls, [], self.now.isoformat())

    def response(self, findings=None, calls=None):
        answer = {"verdict": "RESEARCH", "evidence_ids": ["fixture"], "summary": "受控測試。",
            "risks": [], "missing_data": [], "public_findings": [self.finding] if findings is None else findings}
        return {"status": "completed", "id": "resp_test", "usage": {"input_tokens": 10, "total_tokens": 20},
            "output": [{"type": "web_search_call", **c} for c in (self.calls if calls is None else calls)] +
                [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}]}

    def test_sources_are_frozen_and_tampering_fails_replay(self):
        info = self.info()
        self.assertEqual(info["gaps"], [])
        self.assertFalse(info["clears_context_blockers"])
        verify_public_information(self.core, info)
        info["findings"][0]["fact"] = "tampered"
        with self.assertRaisesRegex(DataError, "INTEGRITY"):
            verify_public_information(self.core, info)

    def test_unseen_url_future_stale_and_unknown_publication_are_excluded(self):
        cases = [("url", "https://invented.example.test/ir", "URL_NOT_IN_HOSTED_SEARCH_TRACE"),
            ("published_at", (self.now + timedelta(minutes=1)).isoformat(), "PUBLICATION_AFTER_CUTOFF"),
            ("published_at", (self.now - timedelta(days=121)).isoformat(), "PUBLICATION_TOO_OLD"),
            ("published_at", None, "PUBLICATION_TIME_UNKNOWN")]
        for key, value, reason in cases:
            with self.subTest(key=key, value=value):
                finding = {**self.finding, key: value}
                info = self.info(finding)
                self.assertFalse(info["findings"])
                self.assertTrue(any(reason in r["reason"] for r in info["rejected_findings"]))
                self.assertIn("FINANCIAL_DISCLOSURE_NOT_RETRIEVED", info["gaps"])

    def test_date_only_today_is_conservatively_unavailable(self):
        from support_scout.contracts import TOKYO
        finding = {**self.finding, "published_at": self.now.astimezone(TOKYO).date().isoformat(), "time_precision": "DATE_ONLY"}
        self.assertFalse(self.info(finding)["findings"])
        finding["published_at"] = (self.now.astimezone(TOKYO) - timedelta(days=1)).date().isoformat()
        self.assertEqual(len(self.info(finding)["findings"]), 1)

    def test_news_cannot_replace_financial_disclosure(self):
        info = self.info({**self.finding, "category": "NEWS", "source_type": "NEWS"})
        self.assertIn("FINANCIAL_DISCLOSURE_NOT_RETRIEVED", info["gaps"])

    def test_synthetic_and_historical_cutoffs_refuse_before_network(self):
        for core in ({**self.core, "synthetic": True}, {**self.core, "market": "TW"},
                     {**self.core, "as_of": (self.now - timedelta(hours=25)).isoformat()}):
            with self.subTest(core=core):
                with self.assertRaises(DataError):
                    validate_live_cutoff(core, self.now.isoformat())
                with self.assertRaises(DataError):
                    OpenAIProvider("test", transport=lambda *a: self.fail("network")).complete(make_packet(core), "reviewer")

    def test_required_tool_and_source_schema_are_sent_without_extra_model_calls(self):
        payloads = []
        def transport(payload, *args):
            payloads.append(payload)
            return self.response()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}):
            review, event = _call(OpenAIProvider("test", transport=transport), self.packet, "researcher", "test")
        self.assertEqual(event["status"], "ACCEPTED", event)
        self.assertEqual(event["web_tool_call_count"], 1)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["tool_choice"], "required")
        self.assertEqual(payloads[0]["max_tool_calls"], 3)
        self.assertEqual(payloads[0]["include"], ["web_search_call.action.sources"])
        self.assertIn("public_findings", payloads[0]["text"]["format"]["schema"]["properties"])
        self.assertEqual(review["public_information"]["findings"][0]["url"], self.finding["url"])
        accepted, errors = validate_reviews(self.packet, [review])
        self.assertFalse(errors)
        self.assertIn("researcher", accepted)

    def test_missing_or_failed_search_is_not_pretended_success(self):
        for calls in ([], [{**self.calls[0], "status": "failed"}], self.calls * 4):
            with self.subTest(calls=calls), patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}):
                result = OpenAIProvider("test", transport=lambda *a: self.response(calls=calls)).complete(self.packet, "reviewer")
                self.assertEqual(result.answer["verdict"], "WATCH")
                self.assertEqual(result.public_information["status"], "DEGRADED")
                self.assertTrue(any("PUBLIC_SEARCH" in gap for gap in result.public_information["gaps"]))
                self.assertEqual(result.public_information["tool_calls"], calls)

    def test_unavailable_financials_force_watch_even_if_model_passes(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}):
            value = OpenAIProvider("test", transport=lambda *a: self.response(findings=[])).complete(self.packet, "reviewer")
        self.assertEqual(value.answer["verdict"], "WATCH")
        self.assertIn("FINANCIAL_DISCLOSURE_NOT_RETRIEVED", value.answer["missing_data"])

    def test_trailing_unexecuted_attempt_preserves_completed_sources_but_keeps_watch(self):
        calls = self.calls * 3 + [{"id": "ignored", "status": "searching", "action": {"type": "open_page",
            "url": "https://never-opened.example.test/future"}}]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}):
            value = OpenAIProvider("test", transport=lambda *a: self.response(calls=calls)).complete(self.packet, "reviewer")
        info = value.public_information
        self.assertEqual(value.answer["verdict"], "WATCH")
        self.assertEqual(info["completed_tool_call_count"], 3)
        self.assertEqual(info["tool_attempt_count"], 4)
        self.assertNotIn("PUBLIC_SEARCH_OVER_BUDGET", info["gaps"])
        self.assertEqual(len(info["findings"]), 1)
        invisible = {**self.finding, "url": "https://never-opened.example.test/future"}
        self.assertFalse(self.info(invisible, calls)["findings"])

    def test_policy_is_versioned_validated_and_disabled_does_not_change_legacy_packet(self):
        with self.assertRaises(DataError):
            load_policy({**load_policy(), "max_tool_calls_per_review": 999})
        bundle = demo_bundle()
        bundle["public_information_policy"] = load_policy()
        with self.assertRaisesRegex(DataError, "SYNTHETIC"):
            run(bundle)
        bundle["public_information_policy"]["enabled"] = False
        self.assertTrue(run(bundle)["agent_packets"])

    def test_public_source_text_is_escaped_in_html(self):
        bundle = demo_bundle()
        report = run(bundle)
        info = self.info({**self.finding, "fact": "<script>bad()</script>"})
        report["securities"][0]["agent_reviews"] = {"researcher": {"verdict": "WATCH", "summary": "test", "public_information": info}}
        html = render_html(report, bundle)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>bad()", html)

    def test_policy_and_search_provenance_are_bound_through_final_decision(self):
        # Saved fabricated data test replay contracts only, never live search.
        from support_scout.contracts import timestamp
        from support_scout.decision import attach_decisions
        bundle = demo_bundle()
        bundle.update(synthetic=False, public_information_policy=load_policy())
        initial = run(bundle)
        packet = initial["agent_packets"][0]
        core = packet["core"]
        finding = {**self.finding, "published_at": (timestamp(core["as_of"]) - timedelta(days=1)).isoformat()}
        info = build_public_information(core, [finding], self.calls, [], core["as_of"])
        reviews = [{"packet_id": packet["packet_id"], "role": role, "agent_id": "fixture-" + role,
            "verdict": "WATCH", "evidence_ids": core["required_confirmation_evidence"],
            "summary": "有來源仍保留缺口。", "risks": [], "missing_data": ["fixture"],
            "produced_at": self.now.isoformat(), "public_information": info} for role in ("researcher", "reviewer")]
        report = run(bundle, reviews=reviews)
        self.assertEqual(set(report["securities"][0]["agent_reviews"]), {"researcher", "reviewer"})
        self.assertNotEqual(report["securities"][0]["trade_decision"]["conclusion"], "CONSIDER")
        report["public_information_policy"]["enabled"] = False
        attach_decisions(report, bundle)
        self.assertIn("SNAPSHOT_INTEGRITY_FAILED", report["securities"][0]["trade_decision"]["reason_codes"])


if __name__ == "__main__":
    unittest.main()
