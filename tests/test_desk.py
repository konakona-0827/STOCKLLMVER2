from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from support_scout.agents import validate_reviews, verify_packet
from support_scout.contracts import DataError, digest, strict_json_loads
from support_scout.demo import demo_bundle
from support_scout.desk import DeskConfig, run_desk
from support_scout.pipeline import run
from support_scout.providers import Completion, OpenAIProvider, ProviderError, post_json


def answer(packet, verdict="RESEARCH"):
    return {"verdict": verdict, "evidence_ids": packet["core"]["required_confirmation_evidence"],
        "summary": "測試替身：固定支撐已回測；收盤跌破下緣即失效。", "risks": ["未驗證樣本外效果"], "missing_data": []}


class FakeProvider:
    mode = "test_transport"
    model = "test-model"

    def __init__(self, verdict="RESEARCH", failure=None, mutate=False):
        self.verdict, self.failure, self.mutate = verdict, failure, mutate
        self.packets = []

    def complete(self, packet, role):
        self.packets.append(deepcopy(packet))
        if self.failure:
            raise self.failure
        if self.mutate:
            packet["core"]["symbol"] = "9999"
        return Completion(answer(packet, self.verdict), "test-response", {"total_tokens": 123})


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.bundle = demo_bundle()

    def test_two_isolated_calls_and_runtime_bound_identities(self):
        researcher, reviewer = FakeProvider(), FakeProvider()
        result = run_desk(self.bundle, researcher=researcher, reviewer=reviewer,
                          config=DeskConfig(max_packets=1))
        self.assertEqual(result["reviewed_shortlist"], ["9001"])
        self.assertEqual(result["desk"]["call_count"], 2)
        self.assertEqual(result["desk"]["status"], "PARTIAL_BUDGET")
        self.assertEqual(researcher.packets, reviewer.packets)
        self.assertNotIn("reviews", researcher.packets[0]["core"])
        ids = [r["agent_id"] for r in result["reviews"]]
        self.assertEqual(len(set(ids)), 2)
        for review, event in zip(result["reviews"], result["desk"]["events"]):
            self.assertTrue(review["agent_id"].startswith(result["desk"]["run_id"]))
            self.assertEqual(event["review_digest"], digest(review))
            self.assertLessEqual(event["started_at"], review["produced_at"])
            self.assertLessEqual(review["produced_at"], event["finished_at"])
        self.assertFalse(result["desk"]["execution_enabled"])
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_model_failure_and_missing_role_stay_watch_with_sanitized_audit(self):
        result = run_desk(self.bundle, researcher=FakeProvider(),
            reviewer=FakeProvider(failure=RuntimeError("secret sk-NEVER-LOG")), config=DeskConfig(1, 2))
        self.assertEqual(result["desk"]["status"], "DEGRADED")
        self.assertEqual(result["desk"]["error_count"], 1)
        self.assertEqual(result["reviewed_shortlist"], [])
        self.assertNotIn("NEVER-LOG", json.dumps(result))
        self.assertEqual(result["securities"][0]["verdict"], "WATCH")

    def test_reviewer_veto_and_hard_gates_override_approvals(self):
        result = run_desk(self.bundle, researcher=FakeProvider(), reviewer=FakeProvider("REJECT"),
                          config=DeskConfig(1, 2))
        self.assertEqual(result["securities"][0]["verdict"], "REJECT")
        result = run_desk(self.bundle, researcher=FakeProvider(), reviewer=FakeProvider())
        self.assertEqual(result["reviewed_shortlist"], ["9001"])
        self.assertEqual(result["securities"][3]["verdict"], "WATCH")
        self.assertEqual(result["securities"][4]["verdict"], "WATCH")

    def test_mutating_provider_cannot_poison_peer_or_original(self):
        original = deepcopy(self.bundle)
        peer = FakeProvider()
        result = run_desk(self.bundle, researcher=FakeProvider(mutate=True), reviewer=peer,
                          config=DeskConfig(1, 2))
        self.assertEqual(peer.packets[0]["core"]["symbol"], "9001")
        self.assertEqual(self.bundle, original)
        self.assertEqual(result["reviewed_shortlist"], [])
        self.assertEqual(result["desk"]["events"][0]["error"], "PACKET_INTEGRITY_FAILED")

    def test_offline_mode_never_claims_research(self):
        with patch.dict(os.environ, {}, clear=True):
            result = run_desk(self.bundle)
        self.assertEqual(result["desk"]["mode"], "offline_simulation")
        self.assertTrue(result["reviews"])
        self.assertTrue(all(r["verdict"] == "WATCH" for r in result["reviews"]))
        self.assertEqual(result["reviewed_shortlist"], [])

    def test_budget_reserves_whole_pairs_and_defers_remaining_packets(self):
        result = run_desk(self.bundle, researcher=FakeProvider(), reviewer=FakeProvider(),
                          config=DeskConfig(5, 3))
        self.assertEqual(result["desk"]["call_count"], 2)
        self.assertEqual(result["desk"]["selected_symbols"], ["9001"])
        self.assertIn("9005", result["desk"]["deferred_symbols"])
        self.assertNotIn("9003", result["desk"]["selected_symbols"])

    def test_complete_candidate_has_priority_over_context_blocked_earlier_symbol(self):
        self.bundle["securities"][4]["symbol"] = "8999"
        result = run_desk(self.bundle, researcher=FakeProvider(), reviewer=FakeProvider(),
                          config=DeskConfig(1, 2))
        self.assertEqual(result["desk"]["selected_symbols"], ["9001"])
        self.assertIn("8999", result["desk"]["deferred_symbols"])
        self.assertEqual(result["reviewed_shortlist"], ["9001"])

    def test_invalid_config_and_future_cutoff(self):
        for kwargs in ({"max_packets": 0}, {"max_packets": True}, {"max_calls": 1}, {"max_calls": 101}):
            with self.assertRaises(DataError):
                DeskConfig(**kwargs)
        with self.assertRaisesRegex(DataError, "MIXED"):
            run_desk(self.bundle, researcher=FakeProvider())
        future = datetime.now(timezone.utc) + timedelta(days=1)
        self.bundle["as_of"] = future.isoformat()
        self.bundle["calendar"]["coverage_through"] = (future + timedelta(days=1)).date().isoformat()
        with self.assertRaisesRegex(DataError, "DESK_CUTOFF_IN_FUTURE"):
            run_desk(self.bundle)


class IntegrityTests(unittest.TestCase):
    def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected(self):
        for raw in ('{"verdict":"REJECT","verdict":"RESEARCH"}', '{"nested":{"x":1,"x":2}}', '{"x":NaN}', '{"x":1e999}'):
            with self.assertRaises(DataError):
                strict_json_loads(raw)

    def test_packet_does_not_alias_input_and_hash_covers_task_and_calendar(self):
        bundle = demo_bundle()
        packet = run(bundle)["agent_packets"][0]
        original = deepcopy(packet)
        bundle["market_context"]["stress"] = "NORMAL"
        bundle["securities"][0]["dossier"]["thesis_status"] = "BROKEN"
        self.assertEqual(packet, original)
        for path in ("tasks", "calendar", "synthetic"):
            modified = deepcopy(packet)
            if path == "tasks":
                modified["tasks"][0]["instruction"] = "do something else"
            elif path == "calendar":
                modified["core"]["calendar"]["source"] = "another-calendar"
            else:
                modified["core"]["synthetic"] = False
            with self.assertRaises(DataError):
                verify_packet(modified)
            self.assertEqual(validate_reviews(modified, [])[1], ["PACKET_INTEGRITY_FAILED"])

    def test_future_review_rejected(self):
        packet = run(demo_bundle())["agent_packets"][0]
        review = {**answer(packet), "packet_id": packet["packet_id"], "role": "researcher",
            "agent_id": "test", "produced_at": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()}
        self.assertIn("REVIEW_TIMESTAMP_IN_FUTURE", validate_reviews(packet, [review])[1])

    def test_malformed_top_level_and_calendar_are_controlled_errors(self):
        for value in ([], None, "bad"):
            with self.assertRaisesRegex(DataError, "BUNDLE_MUST_BE_OBJECT"):
                run(value)
        bundle = demo_bundle()
        bundle["calendar"] = []
        with self.assertRaisesRegex(DataError, "INVALID_CALENDAR"):
            run(bundle)

    def test_locked_prices_in_all_key_stages_stay_watch(self):
        for i in (59, 60, 62, 64, 65, 67, 68):
            with self.subTest(index=i):
                bundle = demo_bundle()
                bundle["securities"][0]["bars"][i]["limit_locked"] = True
                item = run(bundle)["securities"][0]
                if i == 59:  # Not part of pivot/turn evidence: no fabricated blanket rejection.
                    self.assertTrue(item["technical"]["technical_candidate"])
                else:
                    self.assertFalse(item["technical"]["technical_candidate"])
                    self.assertIn("LOCKED_PRICE_IN_SUPPORT_EVIDENCE", item["reasons"])


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.packet = run(demo_bundle())["agent_packets"][0]

    def response(self, value=None, status="completed"):
        return {"id": "resp_test", "status": status, "usage": {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(value or answer(self.packet))}]}]}

    def test_responses_wire_shape_and_independent_contexts(self):
        calls = []
        def transport(payload, key, timeout):
            calls.append(deepcopy(payload))
            self.assertEqual(key, "test-key")
            return self.response()
        provider = OpenAIProvider("explicit-model", transport=transport)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            result = run_desk(demo_bundle(), researcher=provider, reviewer=provider, config=DeskConfig(1, 2))
        self.assertEqual(result["reviewed_shortlist"], ["9001"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["input"], calls[1]["input"])
        self.assertNotEqual(calls[0]["instructions"], calls[1]["instructions"])
        for payload in calls:
            self.assertFalse(payload["store"])
            self.assertNotIn("tools", payload)
            self.assertNotIn("previous_response_id", payload)
            self.assertEqual(payload["text"]["format"]["type"], "json_schema")
            self.assertTrue(payload["text"]["format"]["strict"])
            self.assertNotIn("agent_id", payload["text"]["format"]["schema"]["properties"])

    def test_incomplete_refusal_bad_json_and_extra_identity_do_not_pass(self):
        cases = [self.response(status="incomplete"),
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]},
            self.response({**answer(self.packet), "agent_id": "forged"}),
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "bad json"}]}]}]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            for response in cases:
                with self.subTest(response=response):
                    provider = OpenAIProvider("explicit-model", transport=lambda *args: response)
                    with self.assertRaises(ProviderError):
                        provider.complete(self.packet, "reviewer")

    def test_missing_key_never_calls_transport(self):
        def forbidden(*args):
            self.fail("transport called without key")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ProviderError, "API_KEY_NOT_SET"):
                OpenAIProvider("explicit-model", transport=forbidden).complete(self.packet, "researcher")

    def test_duplicate_verdict_in_api_answer_is_rejected(self):
        response = self.response()
        response["output"][0]["content"][0]["text"] = '{"verdict":"REJECT",' + json.dumps(answer(self.packet))[1:]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with self.assertRaisesRegex(ProviderError, "INVALID_ANSWER_JSON"):
                OpenAIProvider("explicit-model", transport=lambda *args: response).complete(self.packet, "reviewer")

    def test_http_transport_serialization_without_real_network(self):
        response = BytesIO(json.dumps(self.response()).encode())
        class Opener:
            def open(inner, request, timeout):
                self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
                self.assertEqual(request.get_header("Authorization"), "Bearer private-test")
                self.assertEqual(json.loads(request.data), {"model": "test", "store": False})
                self.assertEqual(timeout, 7)
                return response
        with patch("support_scout.providers.build_opener", return_value=Opener()):
            self.assertEqual(post_json({"model": "test", "store": False}, "private-test", 7)["status"], "completed")


class DeskCLITests(unittest.TestCase):
    def test_cli_writes_replay_reviews_and_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable, "-m", "support_scout", "desk-demo", "--out", directory,
                                      "--max-packets", "1"], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(process.returncode, 0, process.stderr)
            root = Path(directory)
            for name in ("input.json", "report.json", "report.md", "agent_tasks.json", "reviews.json", "desk_audit.json"):
                self.assertTrue((root / name).is_file())
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["desk"]["mode"], "offline_simulation")
            self.assertEqual(report["desk"]["call_count"], 2)
            self.assertEqual(report["reviewed_shortlist"], [])

    def test_openai_cli_missing_model_is_controlled_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.json").write_text(json.dumps(demo_bundle()), encoding="utf-8")
            process = subprocess.run([sys.executable, "-m", "support_scout", "desk", str(root / "input.json"),
                                      "--provider", "openai", "--out", str(root / "out")], capture_output=True)
            self.assertEqual(process.returncode, 2)
            self.assertIn(b"EXPLICIT_MODEL_REQUIRED", process.stderr)
            self.assertFalse((root / "out").exists())


if __name__ == "__main__":
    unittest.main()
