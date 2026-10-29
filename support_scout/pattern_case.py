"""Explicit synthetic parameter experiments; never write production observation state."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timedelta
from html import escape
import json
import re
from pathlib import Path
from uuid import uuid4

from .contracts import DataError, Rules, TOKYO, digest, strict_json_loads, timestamp
from .desk import DeskConfig, run_desk
from .discovery import OpenAISearchProvider, make_search_packet, run_search
from .japan import sessions_between
from .patterns import load_registry
from .pipeline import run, render_markdown
from .providers import OpenAIProvider
from .reporting import render_html
from .settings import DEFAULT_SETTINGS, ROOT, load_settings, openai_config
from .storage import publish_run

DEFAULT_CASE = ROOT / "cases" / "advantest_h001.synthetic.json"


def load_case(path=DEFAULT_CASE):
    case = strict_json_loads(Path(path).read_text(encoding="utf-8-sig"))
    if (not isinstance(case, dict) or case.get("schema_version") != 1 or case.get("synthetic") is not True
            or not isinstance(case.get("provenance"), str) or not case["provenance"].strip()
            or not isinstance(case.get("checkpoints"), list) or not case["checkpoints"]):
        raise DataError("EXPLICIT_SYNTHETIC_PATTERN_CASE_REQUIRED")
    ids = [c.get("id") for c in case["checkpoints"]]
    if any(not isinstance(x, str) or not x.isidentifier() for x in ids) or len(ids) != len(set(ids)):
        raise DataError("INVALID_CASE_CHECKPOINT_IDS")
    return case


def build_case_bundle(case, checkpoint_id, *, registry=None):
    checkpoint = next((c for c in case["checkpoints"] if c["id"] == checkpoint_id), None)
    if checkpoint is None or case.get("synthetic") is not True:
        raise DataError("UNKNOWN_OR_NON_SYNTHETIC_CASE")
    cutoff = timestamp(checkpoint["as_of"])
    base = case["baseline"]
    sessions = sessions_between(date.fromisoformat(base["start_session"]), date.fromisoformat(base["end_session"]))
    if cutoff.date().isoformat() not in sessions:
        raise DataError("CASE_CUTOFF_NOT_TRADING_SESSION")
    overrides = {row["session"]: row for row in case["daily_overrides"]}
    if checkpoint.get("bar_override"):
        row = checkpoint["bar_override"]
        overrides[row["session"]] = row
    if not set(overrides) <= set(sessions):
        raise DataError("CASE_OVERRIDE_OUTSIDE_CALENDAR")
    bars = []
    for session in sessions:
        values = {k: base[k] for k in ("open", "high", "low", "close", "volume")}
        values.update({k: v for k, v in overrides.get(session, {}).items() if k != "session"})
        bars.append({"session": session, "closed_at": session + "T15:30:00+09:00",
            "available_at": session + "T16:00:00+09:00", "complete": True,
            **values, "turnover_jpy": values["close"] * values["volume"], "halted": False, "limit_locked": False})
    first = sessions[0] + "T08:00:00+09:00"
    universe = {"universe_id": "synthetic-case-" + digest({"case_id": case["case_id"], "symbol": case["symbol"]})[:20], "symbols": [case["symbol"]],
        "available_at": first, "source": "SYNTHETIC_MANUAL_CASE_NOT_MARKET_DISCOVERY"}
    return {"schema_version": 1, "market": "JP", "synthetic": True, "observation_only": False,
        "as_of": cutoff.isoformat(), "universe_version": universe["universe_id"],
        "discovery_required": True, "source_universe": universe,
        "pattern_registry": load_registry(registry),
        "analysis_context": {"mode": "RETROSPECTIVE_SYNTHETIC_PARAMETER_EXPERIMENT",
            "definition_authored_on": "2026-10-29", "market_data_cutoff": cutoff.isoformat(),
            "claims_configuration_was_available_at_cutoff": False,
            "claims_historical_strategy_performance": False},
        "calendar": {"source": "JPX_VERSIONED_CALENDAR_FOR_SYNTHETIC_CASE", "available_at": first,
            "coverage_through": sessions[-1], "sessions": sessions},
        "market_context": {"source": "SYNTHETIC_MARKET_CONTEXT", "available_at": cutoff.isoformat(), "stress": "NORMAL"},
        "collection": {"status": "COMPLETE", "sources": [], "actual_latest_session": cutoff.date().isoformat()},
        "securities": [{"symbol": case["symbol"], "name": case["name"], "exchange": "TSE", "currency": "JPY",
            "instrument_type": "COMMON_STOCK", "interval": "1d", "volume_unit": "shares",
            "source": "SYNTHETIC_PARAMETER_SCENARIO_NOT_YAHOO_OR_ADVANTEST_HISTORY",
            "price_basis": "raw_no_actions_in_window", "actions_reviewed": True,
            "adjustment_as_of": first, "volume_comparable": True, "tradability_reviewed": True,
            "point_in_time": True, "bars": bars, "pattern_context": deepcopy(case["pattern_context"]),
            "dossier": {"source": "SYNTHETIC_COMPANY_ASSUMPTION", "available_at": cutoff.isoformat(),
                "thesis_status": "INTACT", "events_reviewed_through": (cutoff + timedelta(days=7)).isoformat(), "events": []}}]}


def run_case(*, case_path=DEFAULT_CASE, output_root=None, agents=False, settings_path=DEFAULT_SETTINGS,
             checkpoint_ids=None, max_total_calls=12, search_provider=None, researcher=None, reviewer=None):
    case = load_case(case_path)
    registry = load_registry()
    if type(max_total_calls) is not int or not 0 <= max_total_calls <= 30:
        raise DataError("INVALID_CASE_CALL_BUDGET")
    ids = checkpoint_ids or [c["id"] for c in case["checkpoints"]]
    if len(ids) != len(set(ids)) or not set(ids) <= {c["id"] for c in case["checkpoints"]}:
        raise DataError("INVALID_CASE_CHECKPOINT_SELECTION")
    if agents and (search_provider is None or researcher is None or reviewer is None):
        ai = openai_config(load_settings(Path(settings_path)))
        if not ai["ready"]:
            raise DataError("PATTERN_CASE_OPENAI_CONFIGURATION_REQUIRED")
        search_provider = search_provider or OpenAISearchProvider(ai["model"], max_output_tokens=2400)
        researcher = researcher or OpenAIProvider(ai["model"], max_output_tokens=2400)
        reviewer = reviewer or OpenAIProvider(ai["reviewer_model"], max_output_tokens=2400)
    if agents and len({search_provider.mode, researcher.mode, reviewer.mode}) != 1:
        raise DataError("MIXED_CASE_PROVIDER_MODES")
    values, results = {"case.json": case}, []
    calls = 0
    for identifier in ids:
        bundle = build_case_bundle(case, identifier, registry=registry)
        report = run(bundle)
        search = None
        status = "NUMERIC_ONLY"
        if agents:
            packet = make_search_packet(report, bundle["source_universe"], max_selected=1)
            if max_total_calls - calls < 3 and packet["core"]["candidates"]:
                status = "BUDGET_DEFERRED"
            else:
                search = run_search(packet, search_provider)
                calls += search["call_count"]
                bundle["discovery"] = {"mode": "AGENT_SEARCH", "search": search,
                    "coverage": {"scope": "EXPLICIT_SYNTHETIC_CASE_NOT_AUTONOMOUS_MARKET_SEARCH",
                        "universe_count": 1, "scanned_count": 1}}
                status = "SEARCH_" + search["status"]
                report = run(bundle)
                if search["status"] == "COMPLETED":
                    report = run_desk(bundle, researcher=researcher, reviewer=reviewer,
                        config=DeskConfig(1, 2), selected_symbols=[s["symbol"] for s in search["selection"]])
                    calls += report["desk"]["call_count"]
                    status = "DEGRADED" if report["desk"]["error_count"] else "THREE_AGENT_CASE_COMPLETE"
        item = report["securities"][0]
        patterns = item.get("patterns", [])
        result = {"checkpoint": identifier, "as_of": report["as_of"], "status": status,
            "stages": [{"pattern_id": p["pattern_id"], "stage": p["stage"], "label": p["stage_label"],
                "metrics": p["metrics"], "reason_codes": p["reason_codes"]} for p in patterns],
            "search": search, "reviews": item.get("agent_reviews", {}),
            "trade_conclusion": item["trade_decision"]["conclusion"],
            "expected_stage_is_test_assertion_only": next(c["expected_stage"] for c in case["checkpoints"] if c["id"] == identifier)}
        results.append(result)
        for name, value in (("input", bundle), ("report", report), ("result", result)):
            values[identifier + "_" + name + ".json"] = value
    comparison = {"case_id": case["case_id"], "synthetic": True, "provenance": case["provenance"],
        "agents_requested": agents, "model_call_count": calls, "max_total_calls": max_total_calls,
        "results": results, "empirical_validation": False, "production_ledger_written": False,
        "meaning": "只驗證人造參數與AI解讀；不是Advantest實際買點、真實回測或勝率。"}
    values["comparison.json"] = comparison
    run_id = datetime.now(TOKYO).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:10]
    target = Path(output_root or ROOT / "output" / "pattern-cases") / run_id
    values["manifest.json"] = {"run_id": run_id, "case_digest": digest(case),
        "artifact_digests": {k: digest(v) for k, v in values.items()}, "model_call_count": calls,
        "synthetic": True, "execution_enabled": False}
    files = {k: json.dumps(v, ensure_ascii=False, indent=2, allow_nan=False) + "\n" for k, v in values.items()}
    lines = ["# Advantest H-001 合成參數驗證", "", comparison["meaning"], "", case["provenance"], ""]
    for result in results:
        lines.extend(["## " + result["checkpoint"], "", "截止：" + result["as_of"],
            "階段：" + "、".join(s["label"] for s in result["stages"]), "", "流程：" + result["status"],
            "交易研究：" + result["trade_conclusion"], ""])
        for selection in (result["search"] or {}).get("selection", []):
            lines.extend(["SEARCH：" + selection["rationale"], ""])
        for role, review in result["reviews"].items():
            lines.extend([role + "（" + review["verdict"] + "）：" + review["summary"], "",
                "風險：" + "；".join(review["risks"]), "缺口：" + "；".join(review["missing_data"]), ""])
        files[result["checkpoint"] + "_report.html"] = render_html(values[result["checkpoint"] + "_report.json"], values[result["checkpoint"] + "_input.json"])
    markdown = "\n".join(lines)
    files["comparison.md"] = markdown
    files["comparison.html"] = '<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>H-001 合成驗證</title><style>body{font:16px/1.7 system-ui;max-width:1100px;margin:40px auto;padding:20px}pre{white-space:pre-wrap}</style><h1>Advantest H-001 · 合成參數與 AI 解讀</h1><pre>' + escape(markdown) + '</pre></html>'
    publish_run(target, files)
    return comparison, target


def replay_case(folder):
    """Verify saved case inputs, numbers and review decisions without model calls."""
    folder = Path(folder)
    read = lambda name: strict_json_loads((folder / name).read_text(encoding="utf-8"))
    manifest = read("manifest.json")
    hashes = manifest.get("artifact_digests")
    if not isinstance(hashes, dict) or not {"case.json", "comparison.json"} <= set(hashes):
        raise DataError("PATTERN_CASE_REPLAY_HASHES_REQUIRED")
    for name, expected in hashes.items():
        if name not in {"case.json", "comparison.json"} and not re.fullmatch(r"[A-Za-z0-9_]+_(input|report|result)\.json", name):
            raise DataError("PATTERN_CASE_REPLAY_INVALID_FILENAME")
        if digest(read(name)) != expected:
            raise DataError("PATTERN_CASE_REPLAY_ARTIFACT_MISMATCH")
    case, comparison = read("case.json"), read("comparison.json")
    if case.get("synthetic") is not True or digest(case) != manifest.get("case_digest"):
        raise DataError("PATTERN_CASE_REPLAY_SOURCE_MISMATCH")
    decisions, calls = [], 0
    for result in comparison["results"]:
        identifier = result["checkpoint"]
        required = {identifier + "_" + name + ".json" for name in ("input", "report", "result")}
        if not required <= set(hashes) or read(identifier + "_result.json") != result:
            raise DataError("PATTERN_CASE_REPLAY_RESULT_MISMATCH")
        bundle, original = read(identifier + "_input.json"), read(identifier + "_report.json")
        expected_input = build_case_bundle(case, identifier, registry=bundle["pattern_registry"])
        if {k: v for k, v in bundle.items() if k != "discovery"} != expected_input:
            raise DataError("PATTERN_CASE_REPLAY_INPUT_MISMATCH")
        replayed = run(bundle, as_of=original["as_of"], rules=Rules(**original["rules"]), reviews=original.get("reviews", []))
        fields = ("snapshot_id", "rules", "securities", "technical_queue", "pattern_registry_digest",
                  "pattern_opportunity_queue", "agent_packets", "reviewed_shortlist")
        if any(replayed[k] != original[k] for k in fields):
            raise DataError("PATTERN_CASE_REPLAY_RULE_OR_DECISION_MISMATCH")
        item = replayed["securities"][0]
        stages = [{"pattern_id": p["pattern_id"], "stage": p["stage"], "label": p["stage_label"],
                   "metrics": p["metrics"], "reason_codes": p["reason_codes"]} for p in item["patterns"]]
        if stages != result["stages"] or item["trade_decision"]["conclusion"] != result["trade_conclusion"]:
            raise DataError("PATTERN_CASE_REPLAY_STAGE_MISMATCH")
        calls += (result.get("search") or {}).get("call_count", 0) + original.get("desk", {}).get("call_count", 0)
        decisions.append({"checkpoint": identifier, "stages": [p["stage"] for p in item["patterns"]],
                          "trade_conclusion": item["trade_decision"]["conclusion"]})
    if calls != comparison["model_call_count"] or calls > comparison["max_total_calls"]:
        raise DataError("PATTERN_CASE_REPLAY_USAGE_MISMATCH")
    return {"status": "MATCH", "synthetic": True, "results": decisions, "network_calls": 0, "model_calls": 0}
