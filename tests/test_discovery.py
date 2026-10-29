"""Independent acceptance checks for genuine tool-driven discovery and handoff."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from support_scout.contracts import DataError, digest
from support_scout.demo import demo_bundle
from support_scout.discovery import (PROTOCOL, DiscoveryConfig, OpenAIDiscoveryProvider,
                                     ScoutTask, run_discovery)
from support_scout.discovery_workflow import discover, replay_discovery, demo_inputs
from support_scout.pipeline import run as evaluate
from support_scout.providers import Completion, ProviderError


TASK = ScoutTask("support_finder", "test-finder-v1", "測試任務：先查詢市場，再讀日線，依證據提名。")
OLD_POOL = {"2330", "2454", "2317", "2382", "3231", "3017", "2891", "2881", "2882",
            "2303", "2308", "2345", "2357", "2376", "2356", "2377", "2603", "2609", "1101"}


def inputs(*, synthetic=True):
    base = demo_bundle()
    symbols = ("3552", "6415", "6789")
    bundles, securities = {}, []
    for index, symbol in enumerate(symbols):
        bundle = deepcopy(base)
        bundle["synthetic"] = synthetic
        bundle["securities"] = [deepcopy(base["securities"][0])]
        item = bundle["securities"][0]
        item.update(symbol=symbol, name="市場探索測試 " + symbol)
        if not synthetic:
            bundle["observation_only"] = True
            item.update(observation_only=True, price_basis="raw_unverified", actions_reviewed=False,
                        tradability_reviewed=False, volume_comparable=False, point_in_time=False)
        session = bundle["calendar"]["sessions"][-1]
        bundle["collection"] = {"status": "COMPLETE", "sources": [], "errors": [],
            "expected_latest_session": session, "actual_latest_session": session}
        bundles[symbol] = bundle
        securities.append({"symbol": symbol, "name": item["name"], "industry": "半導體",
            "session": session, "close": item["bars"][-1]["close"],
            "volume_shares": 1_000_000, "turnover_twd": (index + 1) * 50_000_000,
            "change_pct": index - 1.0})
    market = {"schema_version": 1, "market": "TWSE", "synthetic": synthetic,
        "as_of": base["as_of"], "session": base["calendar"]["sessions"][-1],
        "securities": securities, "sources": [], "status": "COMPLETE", "errors": [],
        "coverage": {"eligible_count": len(securities)}, "limitations": []}
    market["catalog_id"] = digest(market)
    return market, bundles


def final_answer(leads=None):
    return {"leads": leads or [], "search_summary": "已查市場工具並依實際回應研究日線。",
            "unexplored": ["其餘未讀取個股的完整日線"]}


def text_response(answer, *, raw=False):
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": answer if raw else json.dumps(answer, ensure_ascii=False)}]}]}


def tool_response(name, arguments, call_id):
    return {"status": "completed", "output": [{"type": "function_call", "name": name,
        "arguments": json.dumps(arguments, ensure_ascii=False), "call_id": call_id}]}


class ChoosingProvider:
    """A model double that chooses from current tool results, never a ticker constant."""
    mode = "test_transport"
    model = "model-fixture"

    def __init__(self, *, sort="turnover_desc", forged_evidence=False):
        self.sort = sort
        self.forged_evidence = forged_evidence
        self.messages = []
        self.tasks = []

    def respond(self, messages, task, config, timeout):
        self.messages.append(deepcopy(messages))
        self.tasks.append(task)
        outputs = [m for m in messages if m.get("type") == "function_call_output"]
        count = len(outputs)
        if count == 0:
            response = tool_response("market_overview", {}, "fixture-0")
        elif count == 1:
            response = tool_response("search_market", {"industry": None, "query": None,
                "sort": self.sort, "min_turnover_twd": 0, "offset": 0, "limit": 3}, "fixture-1")
        elif count == 2:
            rows = json.loads(outputs[-1]["output"])["securities"]
            response = tool_response("inspect_support", {"symbol": rows[0]["symbol"]}, "fixture-2")
        else:
            inspected = json.loads(outputs[-1]["output"])
            response = text_response(final_answer([{"symbol": inspected["symbol"],
                "history_evidence_id": "invented-evidence" if self.forged_evidence else inspected["history_evidence_id"],
                "rationale": "從市場工具發現並讀取日線；形態證據交給雙方研究。", "missing_data": []}]))
        response["usage"] = {"input_tokens": (count + 1) * 7, "output_tokens": 3,
            "total_tokens": (count + 1) * 7 + 3,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 4},
            "output_tokens_details": {"reasoning_tokens": 1}}
        return response


class FailingProvider:
    mode = "test_transport"
    model = "model-fixture"

    def __init__(self, exception):
        self.exception = exception

    def respond(self, *args):
        raise self.exception


class Approver:
    mode = "test_transport"
    model = "review-fixture"

    def __init__(self):
        self.seen = []

    def complete(self, packet, role):
        self.seen.append((role, packet["core"]["symbol"]))
        return Completion({"verdict": "RESEARCH",
            "evidence_ids": packet["core"]["required_confirmation_evidence"],
            "summary": "測試雙方同意仍不能解除原始價觀察限制。", "risks": [], "missing_data": []})


class DiscoveryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.market, self.histories = inputs()
        self.config = DiscoveryConfig()

    def run_provider(self, provider, *, config=None, market=None):
        return run_discovery(market or self.market, lambda symbol: deepcopy(self.histories[symbol]),
            providers={TASK.role: provider}, tasks=[TASK], config=config or self.config)

    def test_tool_loop_selects_outside_old_pool_and_tool_data_changes_selection(self):
        record, repository = self.run_provider(ChoosingProvider())
        first = record["leads"][0]["symbol"]
        self.assertNotIn(first, OLD_POOL)
        self.assertEqual(first, "6789")
        self.assertEqual(list(repository.bundles), [first])
        changed = deepcopy(self.market)
        changed["securities"][0]["turnover_twd"] = 1_000_000_000
        changed["catalog_id"] = digest({k: v for k, v in changed.items() if k != "catalog_id"})
        result, _ = self.run_provider(ChoosingProvider(), market=changed)
        self.assertEqual(result["leads"][0]["symbol"], "3552")
        self.assertNotEqual(result["leads"][0]["symbol"], first)
        self.assertEqual(record["coverage"]["inspected_histories"], 1)
        self.assertFalse(record["coverage"]["full_market_scanned"])

    def test_wire_contract_is_stateless_strict_and_retains_reasoning_items(self):
        calls = []
        model = ChoosingProvider()
        def transport(payload, key, timeout):
            calls.append(deepcopy(payload))
            self.assertEqual(key, "private-test-key")
            response = model.respond(payload["input"], TASK, self.config, timeout)
            if len(calls) == 1:
                response["output"].insert(0, {"type": "reasoning", "id": "reason-1",
                                             "summary": [], "encrypted_content": "fixture-encrypted"})
            return response
        provider = OpenAIDiscoveryProvider("chosen-model", transport=transport)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private-test-key"}):
            record, _ = self.run_provider(provider)
        self.assertEqual(record["scouts"][0]["status"], "COMPLETE")
        self.assertEqual(len(calls), 4)
        for payload in calls:
            self.assertFalse(payload["store"])
            self.assertNotIn("previous_response_id", payload)
            self.assertFalse(payload["parallel_tool_calls"])
            self.assertEqual(payload["include"], ["reasoning.encrypted_content"])
            self.assertEqual(payload["model"], "chosen-model")
            self.assertIn(TASK.instruction, payload["instructions"])
            self.assertEqual({tool["name"] for tool in payload["tools"]},
                             {"market_overview", "search_market", "inspect_support"})
            self.assertTrue(all(tool["strict"] for tool in payload["tools"]))
            self.assertTrue(payload["text"]["format"]["strict"])
            self.assertFalse(payload["text"]["format"]["schema"]["additionalProperties"])
        self.assertTrue(any(row.get("type") == "reasoning" for row in calls[1]["input"]))
        self.assertNotIn("private-test-key", json.dumps(record))

    def test_runtime_binds_each_role_instruction_to_its_own_request(self):
        payloads = []
        provider = OpenAIDiscoveryProvider("chosen-model", transport=lambda payload, *args: payloads.append(payload) or text_response(final_answer()))
        alternate = ScoutTask("sector_finder", "sector-v1", "SECOND-ROLE-ONLY 以產業分散探索。")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private-test-key"}):
            provider.respond([], TASK, self.config, 5)
            provider.respond([], alternate, self.config, 5)
        self.assertIn(TASK.instruction, payloads[0]["instructions"])
        self.assertNotIn("SECOND-ROLE-ONLY", payloads[0]["instructions"])
        self.assertIn("SECOND-ROLE-ONLY", payloads[1]["instructions"])

    def test_nominations_require_market_query_history_and_exact_evidence(self):
        class NoMarketQuery(ChoosingProvider):
            def respond(self, messages, task, config, timeout):
                outputs = [m for m in messages if m.get("type") == "function_call_output"]
                if not outputs:
                    return tool_response("inspect_support", {"symbol": "3552"}, "history-only")
                inspected = json.loads(outputs[-1]["output"])
                return text_response(final_answer([{"symbol": "3552", "history_evidence_id": inspected["history_evidence_id"],
                    "rationale": "未先從市場查到此代號。", "missing_data": []}]))
        class NoHistory(ChoosingProvider):
            def respond(self, messages, task, config, timeout):
                if not any(m.get("type") == "function_call_output" for m in messages):
                    return tool_response("search_market", {"sort": "symbol", "limit": 3}, "market-only")
                return text_response(final_answer([{"symbol": "3552", "history_evidence_id": "not-inspected",
                    "rationale": "只有市場清單，沒有讀日線。", "missing_data": []}]))
        for provider in (NoMarketQuery(), NoHistory(), ChoosingProvider(forged_evidence=True)):
            with self.subTest(provider=type(provider).__name__):
                record, _ = self.run_provider(provider)
                self.assertEqual(record["leads"], [])
                self.assertEqual(record["scouts"][0]["status"], "ERROR")
                self.assertIn("LEAD_WITHOUT_OBSERVED_HISTORY_EVIDENCE", record["scouts"][0]["error"])

    def test_duplicate_tool_call_ids_turn_limit_and_bad_json_fail_closed(self):
        class DuplicateCall(ChoosingProvider):
            def respond(self, messages, *args):
                return tool_response("market_overview", {}, "same-call")
        class BadArguments(ChoosingProvider):
            def respond(self, *args):
                response = tool_response("search_market", {}, "invalid-json")
                response["output"][0]["arguments"] = '{"query":'
                return response
        class BadFinal(ChoosingProvider):
            def respond(self, *args):
                return text_response('{"leads":[],"leads":[]}', raw=True)
        cases = [(DuplicateCall(), self.config, "INVALID_OR_DUPLICATE_TOOL_CALL_ID"),
                 (ChoosingProvider(), replace(self.config, max_turns=2), "DISCOVERY_TURN_LIMIT_EXCEEDED"),
                 (BadArguments(), self.config, "JSONDecodeError"),
                 (BadFinal(), self.config, "DUPLICATE_JSON_KEY")]
        for provider, config, error in cases:
            with self.subTest(error=error):
                record, _ = self.run_provider(provider, config=config)
                self.assertEqual(record["leads"], [])
                self.assertEqual(record["scouts"][0]["error"], error)

    def test_arbitrary_provider_exception_text_is_not_persisted(self):
        for exception in (RuntimeError("private sk-DO-NOT-SAVE"), ProviderError("private sk-DO-NOT-SAVE")):
            with self.subTest(exception=type(exception).__name__):
                record, _ = self.run_provider(FailingProvider(exception))
                self.assertEqual(record["leads"], [])
                self.assertNotIn("DO-NOT-SAVE", json.dumps(record))
                self.assertEqual(record["usage"]["unknown_usage_requests"], 1)

    def test_failed_scout_isolated_from_successful_peer(self):
        peer = ScoutTask("sector_finder", "sector-v1", "探索其他產業，不讀其他代理答案。")
        record, _ = run_discovery(self.market, lambda symbol: deepcopy(self.histories[symbol]),
            providers={TASK.role: FailingProvider(RuntimeError("private-error")), peer.role: ChoosingProvider()},
            tasks=[TASK, peer])
        self.assertEqual(record["status"], "DEGRADED")
        self.assertEqual(record["error_count"], 1)
        self.assertEqual(len(record["leads"]), 1)
        self.assertEqual(record["leads"][0]["nominations"][0]["role"], peer.role)
        self.assertNotIn("private-error", json.dumps(record))

    def test_extensible_scouts_have_isolated_contexts_and_deduplicate_only_at_head(self):
        peer = ScoutTask("sector_finder", "sector-v1", "PEER-ONLY 探索產業線索。")
        first, second = ChoosingProvider(), ChoosingProvider()
        loads = []
        def load(symbol):
            loads.append(symbol)
            return deepcopy(self.histories[symbol])
        record, _ = run_discovery(self.market, load,
            providers={TASK.role: first, peer.role: second}, tasks=[TASK, peer])
        self.assertEqual([s["status"] for s in record["scouts"]], ["COMPLETE", "COMPLETE"])
        self.assertNotEqual(record["scouts"][0]["agent_id"], record["scouts"][1]["agent_id"])
        self.assertEqual(len(record["leads"]), 1)
        self.assertEqual({n["role"] for n in record["leads"][0]["nominations"]}, {TASK.role, peer.role})
        self.assertEqual(loads, ["6789"])
        self.assertEqual(first.messages[0], second.messages[0])
        self.assertNotIn("PEER-ONLY", json.dumps(first.messages, ensure_ascii=False))
        self.assertTrue(all(task.role == TASK.role for task in first.tasks))
        self.assertTrue(all(task.role == peer.role for task in second.tasks))

    def test_catalog_tampering_is_rejected_before_any_model_call(self):
        altered = deepcopy(self.market)
        altered["securities"][0]["close"] = 987
        provider = ChoosingProvider()
        with self.assertRaisesRegex(DataError, "CATALOG_INTEGRITY_FAILED"):
            self.run_provider(provider, market=altered)
        self.assertEqual(provider.messages, [])

    def test_empty_answer_without_any_market_query_is_not_completed_discovery(self):
        class NoExploration(ChoosingProvider):
            def respond(self, *args):
                return text_response(final_answer())
        record, _ = self.run_provider(NoExploration())
        self.assertEqual(record["status"], "DEGRADED")
        self.assertEqual(record["scouts"][0]["status"], "ERROR")
        self.assertEqual(record["scouts"][0]["error"], "DISCOVERY_MARKET_NOT_EXPLORED")
        self.assertEqual(record["coverage"]["viewed_symbols"], 0)

    def test_usage_is_accumulated_for_every_model_turn_with_billing_details(self):
        record, _ = self.run_provider(ChoosingProvider())
        self.assertEqual(record["usage"], {"request_count": 4, "input_tokens": 70,
            "output_tokens": 12, "unknown_usage_requests": 0})
        events = record["scouts"][0]["requests"]
        self.assertEqual([e["turn"] for e in events], [1, 2, 3, 4])
        self.assertTrue(all(e["usage"]["input_tokens_details"]["cache_write_tokens"] == 4 for e in events))
        self.assertTrue(all(e["usage"]["output_tokens_details"]["reasoning_tokens"] == 1 for e in events))


class DiscoveryWorkflowTests(unittest.TestCase):
    def test_history_source_failure_is_degraded_instead_of_success_with_no_leads(self):
        from support_scout.discovery_workflow import OfflineDiscoveryProvider
        market, _ = demo_inputs()
        def unavailable(symbol):
            raise DataError("TWSE_NETWORK_UNAVAILABLE")
        with tempfile.TemporaryDirectory() as folder:
            record, report, target, code = discover(demo=True, market=market,
                history_loader=unavailable, providers={"support_finder": OfflineDiscoveryProvider()},
                output_root=Path(folder) / "out")
        self.assertEqual(record["status"], "DEGRADED")
        self.assertEqual(record["tool_error_count"], 1)
        self.assertEqual(record["coverage"]["inspected_histories"], 0)
        self.assertEqual(record["leads"], [])
        self.assertEqual(code, 3)
        self.assertIsNone(report)

    def test_configuration_failure_records_attempt_without_replacing_previous_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, _, target, _ = discover(demo=True, output_root=root)
            previous = (root / "latest_discovery.json").read_bytes()
            with self.assertRaisesRegex(DataError, "INVALID_DISCOVERY_LIMIT"):
                discover(demo=True, output_root=root, max_leads=0)
            attempt = json.loads((root / "latest_attempt.json").read_text(encoding="utf-8"))
            self.assertEqual(attempt["status"], "FAILED")
            self.assertEqual((root / "latest_discovery.json").read_bytes(), previous)
            self.assertEqual(len(list((root / "failures").glob("*.json"))), 1)
            self.assertTrue((target / "report.html").is_file())

    def live_run(self, root, **overrides):
        market, histories = inputs(synthetic=False)
        plan_path = root / "plan.json"
        plan_path.write_text(json.dumps({"version": PROTOCOL, "scouts": [asdict(TASK)],
            "limits": asdict(DiscoveryConfig())}), encoding="utf-8")
        with patch("support_scout.twse.TwseClient.get", side_effect=AssertionError("network called")), \
             patch("support_scout.discovery_workflow.openai_config", side_effect=AssertionError("credentials loaded")):
            return discover(plan_path=plan_path, settings_path=root / "missing.json",
                output_root=root / "out", data_root=root / "data", market=market,
                history_loader=lambda symbol: deepcopy(histories[symbol]),
                providers={TASK.role: ChoosingProvider()}, researcher=Approver(), reviewer=Approver(), **overrides)

    def test_demo_and_replay_are_fully_offline_and_keep_three_agent_roles(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("support_scout.discovery.OpenAIDiscoveryProvider.respond", side_effect=AssertionError("model called")), \
                 patch("support_scout.providers.OpenAIProvider.complete", side_effect=AssertionError("review model called")), \
                 patch("support_scout.twse.TwseClient.get", side_effect=AssertionError("network called")), \
                 patch("support_scout.discovery_workflow.openai_config", side_effect=AssertionError("credentials loaded")):
                record, report, target, code = discover(demo=True, output_root=Path(folder) / "out")
                replay = replay_discovery(target)
            self.assertEqual(code, 0)
            self.assertEqual([s["role"] for s in record["scouts"]], ["support_finder"])
            self.assertEqual(report["desk"]["call_count"], 2)
            self.assertEqual({e["role"] for e in report["desk"]["events"]}, {"researcher", "reviewer"})
            self.assertEqual(report["reviewed_shortlist"], [])
            self.assertEqual(replay["status"], "MATCH")
            self.assertEqual((replay["network_calls"], replay["model_calls"]), (0, 0))
            for name in ("market.json", "discovery.json", "discovery_histories.json", "discovery_plan.json",
                         "manifest.json", "report.html", "report.json", "completed.json"):
                self.assertTrue((target / name).is_file(), name)
            self.assertFalse((target / "committed.json").exists())

    def test_plan_can_add_a_second_scout_without_changing_runtime_or_review_roles(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            peer = ScoutTask("sector_finder", "sector-v1", "尋找不同產業的支撐線索。")
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({"version": PROTOCOL,
                "scouts": [asdict(TASK), asdict(peer)], "limits": asdict(DiscoveryConfig())}), encoding="utf-8")
            record, report, target, code = discover(demo=True, plan_path=plan_path, output_root=root / "out")
            self.assertEqual(code, 0)
            self.assertEqual({s["role"] for s in record["scouts"]}, {TASK.role, peer.role})
            self.assertEqual(len(record["leads"]), 1)
            self.assertEqual(len(record["leads"][0]["nominations"]), 2)
            self.assertEqual(report["desk"]["call_count"], 2)
            self.assertEqual(replay_discovery(target)["status"], "MATCH")

    def test_saved_discovery_artifact_tampering_is_rejected_by_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            record, report, target, code = discover(demo=True, output_root=Path(folder) / "out")
            path = target / "discovery.json"
            saved = json.loads(path.read_text(encoding="utf-8"))
            saved["scouts"][0]["search_summary"] = "竄改後的探索紀錄"
            path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(DataError, "ARTIFACT_INTEGRITY_FAILED"):
                replay_discovery(target)

    def test_live_input_price_tampering_is_rejected_even_when_verdict_stays_watch(self):
        with tempfile.TemporaryDirectory() as folder:
            record, report, target, code = self.live_run(Path(folder))
            path = target / "input.json"
            altered = json.loads(path.read_text(encoding="utf-8"))
            latest = altered["securities"][0]["bars"][-1]
            latest["close"] += 0.0001 if latest["close"] < latest["high"] else -0.0001
            # Reproduce the old false-positive: changed evidence can still yield WATCH.
            recalculated = evaluate(altered, reviews=report.get("reviews", []))
            self.assertEqual(recalculated["securities"][0]["verdict"], report["securities"][0]["verdict"])
            self.assertEqual(report["securities"][0]["verdict"], "WATCH")
            path.write_text(json.dumps(altered, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(DataError, "HANDOFF_INPUT_MISMATCH"):
                replay_discovery(target)

    def test_live_report_technical_tampering_cannot_pass_by_preserving_verdict(self):
        with tempfile.TemporaryDirectory() as folder:
            record, report, target, code = self.live_run(Path(folder))
            path = target / "report.json"
            altered = json.loads(path.read_text(encoding="utf-8"))
            altered["securities"][0]["technical"]["latest_close"] += 0.0001
            self.assertEqual(altered["securities"][0]["verdict"], "WATCH")
            path.write_text(json.dumps(altered, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(DataError, "OBSERVATION_INTEGRITY_FAILED"):
                replay_discovery(target)
            # Even a recomputed envelope hash must not replace deterministic review replay.
            manifest_path = target / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["report_digest"] = digest(altered)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(DataError, "REVIEW_MISMATCH"):
                replay_discovery(target)

    def test_replay_requires_correct_completion_marker_for_demo_and_live_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            live_root = root / "live"
            live_root.mkdir()
            _, _, live_target, _ = self.live_run(live_root)
            _, _, demo_target, _ = discover(demo=True, output_root=root / "demo")
            for mode, base, marker_name, flag in (
                ("live", live_target, "committed.json", "ledger_committed"),
                ("demo", demo_target, "completed.json", "ledger_written"),
            ):
                for kind in ("missing", "wrong_run_id", "wrong_commit_state"):
                    with self.subTest(mode=mode, kind=kind):
                        target = root / (mode + "-" + kind)
                        shutil.copytree(base, target)
                        marker_path = target / marker_name
                        if kind == "missing":
                            marker_path.unlink()
                            expected = "RUN_NOT_COMPLETED"
                        else:
                            marker = json.loads(marker_path.read_text(encoding="utf-8"))
                            if kind == "wrong_run_id":
                                marker["run_id"] = "different-run"
                            else:
                                marker[flag] = not marker[flag]
                            marker_path.write_text(json.dumps(marker), encoding="utf-8")
                            expected = "INVALID_COMPLETION_MARKER"
                        with self.assertRaisesRegex(DataError, expected):
                            replay_discovery(target)

    def test_removing_manifest_hashes_cannot_disable_replay_integrity_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            live_root = root / "live"
            live_root.mkdir()
            _, _, live_target, _ = self.live_run(live_root)
            _, _, demo_target, _ = discover(demo=True, output_root=root / "demo")
            for mode, base, hash_field in (
                ("live", live_target, "discovery_artifact_digests"),
                ("demo", demo_target, "artifact_digests"),
            ):
                for kind in ("entire_map", "required_history_hash"):
                    with self.subTest(mode=mode, kind=kind):
                        target = root / (mode + "-" + kind)
                        shutil.copytree(base, target)
                        manifest_path = target / "manifest.json"
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                        if kind == "entire_map":
                            manifest.pop(hash_field)
                        else:
                            manifest[hash_field].pop("discovery_histories.json")
                        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                        with self.assertRaisesRegex(DataError, "MANIFEST_DIGESTS_REQUIRED"):
                            replay_discovery(target)
            for field in ("input_digest", "rules_hash", "report_digest", "snapshot_id"):
                with self.subTest(observation_hash=field):
                    target = root / ("live-missing-" + field)
                    shutil.copytree(live_target, target)
                    manifest_path = target / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest.pop(field)
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaisesRegex(DataError, "INTEGRITY_FAILED|SNAPSHOT_MISMATCH"):
                        replay_discovery(target)

    def test_discovery_overrides_are_saved_as_effective_plan_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            record, report, target, code = self.live_run(root, max_leads=1, max_history_symbols=1,
                                                       max_packets=1, max_calls=2)
            self.assertEqual(code, 0)
            saved = json.loads((target / "discovery_plan.json").read_text(encoding="utf-8"))
            source = json.loads((root / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["limits"]["max_leads"], 1)
            self.assertEqual(saved["limits"]["max_history_symbols"], 1)
            self.assertEqual(record["config"], saved["limits"])
            self.assertEqual(source["limits"]["max_leads"], DiscoveryConfig().max_leads)
            self.assertEqual(source["limits"]["max_history_symbols"], DiscoveryConfig().max_history_symbols)
            self.assertEqual(report["desk"]["call_count"], 2)
            self.assertEqual(replay_discovery(target)["status"], "MATCH")

    def test_only_evidence_bound_nominations_reach_raw_observation_and_both_reviewers(self):
        market, histories = inputs(synthetic=False)
        model, research, review = ChoosingProvider(), Approver(), Approver()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = {"version": PROTOCOL, "scouts": [asdict(TASK)], "limits": asdict(DiscoveryConfig())}
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with patch("support_scout.twse.TwseClient.get", side_effect=AssertionError("network called")), \
                 patch("support_scout.discovery_workflow.openai_config", side_effect=AssertionError("credentials loaded")):
                record, report, target, code = discover(plan_path=plan_path, settings_path=root / "missing.json",
                    output_root=root / "out", data_root=root / "data", market=market,
                    history_loader=lambda symbol: deepcopy(histories[symbol]),
                    providers={TASK.role: model}, researcher=research, reviewer=review)
                replay = replay_discovery(target)
            selected = {lead["symbol"] for lead in record["leads"]}
            self.assertEqual(selected, {"6789"})
            self.assertEqual({s["symbol"] for s in report["securities"]}, selected)
            self.assertEqual(research.seen, [("researcher", "6789")])
            self.assertEqual(review.seen, [("reviewer", "6789")])
            self.assertEqual(report["securities"][0]["verdict"], "WATCH")
            self.assertIn("OBSERVATION_ONLY", report["securities"][0]["reasons"])
            self.assertEqual(report["reviewed_shortlist"], [])
            self.assertTrue((target / "committed.json").is_file())
            self.assertTrue((target / "discovery.json").is_file())
            self.assertEqual(code, 0)
            self.assertEqual(replay["status"], "MATCH")

    def test_cli_demo_and_saved_transcript_replay_need_no_network(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "out"
            first = subprocess.run([sys.executable, "-m", "support_scout", "discovery-demo", "--out", str(output),
                "--max-leads", "1", "--max-history-symbols", "1", "--max-packets", "1", "--max-calls", "2"],
                capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(first.returncode, 0, first.stderr)
            runs = list(output.glob("*/discovery.json"))
            self.assertEqual(len(runs), 1)
            saved_plan = json.loads((runs[0].parent / "discovery_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_plan["limits"]["max_leads"], 1)
            self.assertEqual(saved_plan["limits"]["max_history_symbols"], 1)
            second = subprocess.run([sys.executable, "-m", "support_scout", "discovery-replay", str(runs[0].parent)],
                capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("MATCH", second.stdout)


if __name__ == "__main__":
    unittest.main()
