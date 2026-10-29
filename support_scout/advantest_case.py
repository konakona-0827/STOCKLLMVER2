"""Current-vintage real-price case reconstruction, never a historical PIT backtest.

Original Yahoo retrieval timestamps stay unchanged. Each prefix has a separate
historical market-data cutoff; today's analysis time is never called an old
trading decision. No account state, production ledger, or historical web search.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, time
from html import escape
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from .contracts import DataError, Rules, TOKYO, digest, strict_json_loads, timestamp
from .desk import DeskConfig, run_desk, _call
from .discovery import OpenAISearchProvider, make_search_packet, run_search
from .japan import build_snapshot
from .patterns import load_registry
from .pipeline import run
from .providers import OpenAIProvider
from .reporting import render_html
from .settings import ROOT, DEFAULT_SETTINGS, load_settings, openai_config
from .storage import atomic_json, publish_run

DEFAULT_REAL_CASE = ROOT / "cases" / "advantest_h001.real.json"


def load_real_case(path=DEFAULT_REAL_CASE):
    value = strict_json_loads(Path(path).read_text(encoding="utf-8-sig"))
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("real_market_data") is not True or value.get("symbol") != "6857.T"):
        raise DataError("ADVANTEST_REAL_CASE_REQUIRED")
    if date.fromisoformat(value["evaluation_start"]) > date.fromisoformat(value["evaluation_end"]):
        raise DataError("REAL_CASE_INVALID_WINDOW")
    return value


def fetch_official_documents(case, cache_dir, *, opener=None, now=None, timeout=15):
    """Archive reviewed official source files; do not pretend to parse/verify PDFs."""
    documents = []
    folder = Path(cache_dir)
    folder.mkdir(parents=True, exist_ok=True)
    for definition in case["official_documents"]:
        url = definition["url"]
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "www.advantest.com" or not parsed.path.endswith(".pdf"):
            raise DataError("REAL_CASE_OFFICIAL_DOCUMENT_URL_REQUIRED")
        result = deepcopy(definition)
        try:
            with (opener or urlopen)(Request(url, headers={"User-Agent": "SupportDeskJapan/0.7 read-only research"}), timeout=timeout) as response:
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000 or not raw.startswith(b"%PDF"):
                raise DataError("REAL_CASE_INVALID_OFFICIAL_PDF")
            checksum = hashlib.sha256(raw).hexdigest()
            target = folder / (definition["id"] + "-" + checksum[:16] + ".pdf")
            target.write_bytes(raw)
            result.update(status="ARCHIVED", sha256=checksum, raw_file=str(target.resolve()))
        except (OSError, ValueError) as exc:
            # Availability failures are data gaps, never fabricated source facts.
            result.update(status="UNAVAILABLE", error=type(exc).__name__, facts=[])
        fetched = now() if callable(now) else now or datetime.now(TOKYO)
        result["fetched_at"] = fetched.isoformat()
        documents.append(result)
    return documents


def checkpoint_definitions(case, source_bundle):
    end = case["evaluation_end"]
    first = case["evaluation_start"]
    available = [r["session"] for r in source_bundle["securities"][0]["bars"]]
    if not available or available[-1] < end:
        raise DataError("REAL_CASE_REQUIRED_END_SESSION_UNAVAILABLE")
    result = [{"id": "d" + day.replace("-", ""), "market_data_cutoff": day + "T16:00:00+09:00"}
              for day in available if first <= day <= end]
    result.extend(deepcopy(case.get("additional_checkpoints", [])))
    ids = [r["id"] for r in result]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r"[A-Za-z0-9_]+", identifier) for identifier in ids):
        raise DataError("REAL_CASE_CHECKPOINT_IDS_INVALID")
    return result


def _visible_documents(documents, historical_cutoff):
    result = []
    for document in documents:
        # Exact release time is unknown. Conservatively allow a DATE_ONLY file
        # after that day's end, explicitly as an assumption, not a release time.
        known_after = datetime.combine(date.fromisoformat(document["published_date"]), time.max, TOKYO)
        if known_after <= historical_cutoff and document.get("status") == "ARCHIVED":
            visible = {k: deepcopy(v) for k, v in document.items() if k != "raw_file"}
            visible["effective_known_after"] = known_after.isoformat()
            visible["timing_assumption"] = "DATE_ONLY_CONSERVATIVE_END_OF_DAY_NOT_VERIFIED_RELEASE_TIME"
            result.append(visible)
    return result


def build_real_bundle(source_bundle, case, checkpoint, *, documents=None, registry=None,
                      communication_policy=None, evaluated_at=None):
    if (source_bundle.get("synthetic") is not False or source_bundle.get("market") != "JP"
            or source_bundle.get("observation_only") is not True
            or len(source_bundle.get("securities", [])) != 1
            or source_bundle["securities"][0].get("symbol") != case["symbol"]):
        raise DataError("REAL_CASE_REQUIRES_UNALTERED_SINGLE_YAHOO_OBSERVATION")
    documents = documents or []
    cutoff = timestamp(checkpoint["market_data_cutoff"])
    latest_retrieval = max([timestamp(source_bundle["as_of"]),
        *[timestamp(d["fetched_at"]) for d in documents]])
    evaluated = timestamp(evaluated_at) if evaluated_at else latest_retrieval
    if evaluated < latest_retrieval or cutoff > evaluated:
        raise DataError("REAL_CASE_EVALUATION_PRECEDES_SOURCE_OR_CUTOFF")
    bundle = deepcopy(source_bundle)
    item = bundle["securities"][0]
    item["bars"] = [r for r in item["bars"] if r["session"] <= cutoff.astimezone(TOKYO).date().isoformat()]
    if len(item["bars"]) < 60 or item["bars"][-1]["session"] != cutoff.date().isoformat():
        raise DataError("REAL_CASE_PREFIX_REQUIRES_60_COMPLETE_BARS_AND_TARGET_SESSION")
    if timestamp(item["bars"][-1]["closed_at"]) > cutoff:
        raise DataError("REAL_CASE_CUTOFF_BEFORE_COMPLETE_DAILY_BAR")
    item["point_in_time"] = False
    item["observation_blockers"] = list(dict.fromkeys([*item.get("observation_blockers", []),
        "RETROSPECTIVE_CURRENT_VINTAGE_RECONSTRUCTION"]))
    item["pattern_context"] = {"catalysts": [], "decline_driver": {}}
    item["dossier"] = {}
    bundle["as_of"] = evaluated.isoformat()
    bundle["calendar"]["sessions"] = [s for s in bundle["calendar"]["sessions"] if s <= item["bars"][-1]["session"]]
    bundle["calendar"]["coverage_through"] = evaluated.astimezone(TOKYO).date().isoformat()
    bundle["calendar"]["scope"] = "TRUNCATED_RETROSPECTIVE_CASE_NOT_LIVE_FRESHNESS_CALENDAR"
    bundle["collection"].update(expected_latest_session=item["bars"][-1]["session"],
        actual_latest_session=item["bars"][-1]["session"], mode="RETROSPECTIVE_SINGLE_SYMBOL_PREFIX")
    bundle["market_context"] = {}
    bundle["pattern_registry"] = load_registry(registry)
    # Historical network research is always off. A separate current observation
    # may use web_search, but cannot be mixed into these old data prefixes.
    bundle.pop("public_information_policy", None)
    bundle.pop("portfolio_snapshot", None)
    from .communication import load_policy
    bundle["communication_policy"] = load_policy(communication_policy)
    visible = _visible_documents(documents, cutoff)
    bundle["analysis_context"] = {"mode": "RETROSPECTIVE_REAL_PRICE_CASE_RECONSTRUCTION",
        "case_id": case["case_id"], "checkpoint": checkpoint["id"],
        "market_data_cutoff": cutoff.isoformat(), "source_retrieved_at": source_bundle["as_of"],
        "analysis_performed_at": evaluated.isoformat(), "parameters_authored_on": case["parameters_authored_on"],
        "historical_point_in_time_verified": False, "claims_historical_strategy_performance": False,
        "selection_scope": "USER_NOMINATED_SINGLE_STOCK_NOT_MARKET_DISCOVERY_HIT_RATE",
        "visible_official_facts": visible,
        "public_information_gaps": ["NO_PRE_EVENT_SAME_PERIOD_MARKET_CONSENSUS",
            "DATE_ONLY_OFFICIAL_RELEASE_TIME", "CURRENT_DOWNLOAD_NOT_HISTORICAL_ARCHIVE",
            "COMPANY_GUIDANCE_REVISION_IS_NOT_CONSENSUS_SURPRISE"],
        "instruction": "只用market_data_cutoff前保留的日線和visible_official_facts。今天的取得時間不是歷史交易時點，不能使用未提供的之後價格或公告；這是對真實資料前綴的事後案例分析。"}
    universe = {"universe_id": "advantest-real-case-" + digest({"case": case["case_id"],
        "source": source_bundle["collection"].get("sources", [])})[:20], "symbols": [case["symbol"]],
        "available_at": evaluated.isoformat(), "scope": "USER_NOMINATED_SINGLE_REAL_STOCK"}
    bundle["source_universe"] = universe
    bundle["discovery_required"] = True
    bundle["universe_version"] = universe["universe_id"]
    bundle.pop("discovery", None)
    return bundle


def _diagnostic_packet(report):
    packets = report.get("all_packets", report.get("agent_packets", []))
    return next((p for p in packets if p["core"]["symbol"] == "6857.T"), None)


def _summary_item(report, checkpoint, status, search, diagnostic_reviews, diagnostic_audit):
    item = report["securities"][0]
    pattern = next((p for p in item.get("patterns", []) if p["pattern_id"] == "H-001"), {})
    return {"checkpoint": checkpoint["id"], "market_data_cutoff": checkpoint["market_data_cutoff"],
        "analysis_as_of": report["as_of"], "status": status,
        "latest_session": item.get("technical", {}).get("latest_session"),
        "latest_close": item.get("technical", {}).get("latest_close"),
        "technical_state": item.get("technical", {}).get("state"),
        "pattern_stage": pattern.get("stage"), "pattern_research_candidate": pattern.get("research_candidate", False),
        "pattern_metrics": deepcopy(pattern.get("metrics", {})), "pattern_reasons": pattern.get("reason_codes", []),
        "search_pool_count": len(((search or {}).get("search_packet") or {}).get("core", {}).get("candidates", [])),
        "search_selected": [s["symbol"] for s in (search or {}).get("selection", [])],
        "standard_reviews": deepcopy(item.get("agent_reviews", {})),
        "diagnostic_reviews": deepcopy(diagnostic_reviews), "diagnostic_audit": deepcopy(diagnostic_audit),
        "trade_conclusion": item["trade_decision"]["conclusion"],
        "trade_reasons": item["trade_decision"]["reason_codes"],
        "meaning": "真實行情前綴重算；非當時PIT決策。診斷評語不算SEARCH命中，不能改交易閘門。"}


def run_real_case(*, case_path=DEFAULT_REAL_CASE, output_root=None, source_bundle=None,
                  documents=None, agents=False, checkpoint_ids=None, max_total_calls=9,
                  diagnostic_review=False, settings_path=DEFAULT_SETTINGS,
                  search_provider=None, researcher=None, reviewer=None):
    case = load_real_case(case_path)
    if type(max_total_calls) is not int or not 0 <= max_total_calls <= 30:
        raise DataError("REAL_CASE_INVALID_CALL_BUDGET")
    if type(diagnostic_review) is not bool:
        raise DataError("REAL_CASE_DIAGNOSTIC_FLAG_REQUIRED")
    source_folder = ROOT / "local_data" / "advantest-real"
    if source_bundle is None:
        source_bundle = build_snapshot([case["symbol"]], source_folder / "yahoo", months=8, timeout=15)
    if documents is None:
        documents = fetch_official_documents(case, source_folder / "official")
    registry = load_registry()
    from .communication import load_policy
    communication = load_policy()
    checkpoints = checkpoint_definitions(case, source_bundle)
    ids = checkpoint_ids or [c["id"] for c in checkpoints]
    if len(ids) != len(set(ids)) or not set(ids) <= {c["id"] for c in checkpoints}:
        raise DataError("REAL_CASE_INVALID_CHECKPOINT_SELECTION")
    if agents and (search_provider is None or researcher is None or reviewer is None):
        ai = openai_config(load_settings(Path(settings_path)))
        if not ai["ready"]:
            raise DataError("REAL_CASE_OPENAI_CONFIGURATION_REQUIRED")
        search_provider = search_provider or OpenAISearchProvider(ai["model"], max_output_tokens=1600)
        researcher = researcher or OpenAIProvider(ai["model"], max_output_tokens=2000)
        reviewer = reviewer or OpenAIProvider(ai["reviewer_model"], max_output_tokens=2000)
    if agents and len({search_provider.mode, researcher.mode, reviewer.mode}) != 1:
        raise DataError("REAL_CASE_MIXED_PROVIDER_MODES")
    artifacts = {"case.json": case, "source_bundle.json": source_bundle, "official_documents.json": documents}
    results, calls = [], 0
    for checkpoint in checkpoints:
        if checkpoint["id"] not in ids:
            continue
        bundle = build_real_bundle(source_bundle, case, checkpoint, documents=documents,
            registry=registry, communication_policy=communication)
        report = run(bundle)
        search, diagnostic_reviews, diagnostic_audit = None, {}, []
        status = "NUMERIC_ONLY"
        if agents:
            search_packet = make_search_packet(report, bundle["source_universe"], max_selected=1)
            if search_packet["core"]["candidates"] and max_total_calls - calls < 3:
                status = "BUDGET_DEFERRED"
            else:
                search = run_search(search_packet, search_provider)
                calls += search["call_count"]
                bundle["discovery"] = {"mode": "AGENT_SEARCH", "search": search,
                    "coverage": {"universe_count": 1, "scanned_count": 1,
                        "scope": "USER_NOMINATED_REAL_CASE_NOT_AUTONOMOUS_MARKET_HIT"},
                    "max_total_calls": max_total_calls, "total_call_count": search["call_count"]}
                report = run(bundle)
                status = "SEARCH_" + search["status"]
                if search["status"] == "COMPLETED":
                    report = run_desk(bundle, researcher=researcher, reviewer=reviewer,
                        config=DeskConfig(1, 2), selected_symbols=[s["symbol"] for s in search["selection"]])
                    calls += report["desk"]["call_count"]
                    bundle["discovery"]["total_call_count"] += report["desk"]["call_count"]
                    report["discovery"] = deepcopy(bundle["discovery"])
                    status = "THREE_AGENT_CASE_COMPLETE" if not report["desk"]["error_count"] else "THREE_AGENT_CASE_DEGRADED"
                elif diagnostic_review and search["status"] == "ABSTAIN":
                    packet = _diagnostic_packet(report)
                    if packet is not None and max_total_calls - calls >= 2:
                        for role, provider in (("researcher", researcher), ("reviewer", reviewer)):
                            review, event = _call(provider, packet, role, "real-case-diagnostic-" + uuid4().hex)
                            calls += 1
                            diagnostic_audit.append(event)
                            if review is not None:
                                diagnostic_reviews[role] = review
                        status = "DIAGNOSTIC_REVIEW_NOT_SEARCH_HIT"
                        report["diagnostic_reviews"] = deepcopy(diagnostic_reviews)
                        report["diagnostic_audit"] = deepcopy(diagnostic_audit)
                    elif packet is not None:
                        status = "DIAGNOSTIC_BUDGET_DEFERRED"
                    else:
                        status = "NUMERIC_GATE_NO_PACKET_NO_AI_REVIEW"
        result = _summary_item(report, checkpoint, status, search, diagnostic_reviews, diagnostic_audit)
        results.append(result)
        prefix = checkpoint["id"]
        artifacts[prefix + "_input.json"] = bundle
        artifacts[prefix + "_report.json"] = report
        artifacts[prefix + "_result.json"] = result
    if calls > max_total_calls:
        raise DataError("REAL_CASE_TOTAL_CALL_BUDGET_EXCEEDED")
    comparison = {"case_id": case["case_id"], "synthetic": False, "observation_only": True,
        "analysis_mode": "CURRENT_VINTAGE_HISTORICAL_PREFIX_RECONSTRUCTION",
        "agents_requested": agents, "diagnostic_review": diagnostic_review,
        "model_call_count": calls, "max_total_calls": max_total_calls, "results": results,
        "empirical_validation": False, "production_ledger_written": False,
        "meaning": "指定Advantest單案例的事後重算與AI診斷；不是歷史當時決策、全市場命中率或策略準確率。"}
    artifacts["comparison.json"] = comparison
    run_id = datetime.now(TOKYO).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:10]
    target = Path(output_root or ROOT / "output" / "advantest-real-cases") / run_id
    artifacts["manifest.json"] = {"run_id": run_id, "case_digest": digest(case),
        "artifact_digests": {name: digest(value) for name, value in artifacts.items()},
        "source_code_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/"support_scout").glob("*.py"))},
        "synthetic": False, "historical_point_in_time_verified": False, "execution_enabled": False}
    files = {name: json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n" for name, value in artifacts.items()}
    lines = ["# Advantest 真實行情案例檢查", "", comparison["meaning"], "",
        "原始資料保留實際取得時間；歷史截點只截斷行情前綴，不改成歷史當時可得。日線不能辨識盤中先後。",
        "官方公司預測上修不等於超市場預期；缺事前一致預期則不宣稱正面驚喜。", "",
        "|行情截止|H-001|支撐穩定期數|距支撐|SEARCH提交|交易研究|流程|",
        "|---|---|---:|---:|---|---|---|"]
    for result in results:
        m = result["pattern_metrics"]
        distance = m.get("distance_from_support_fraction")
        lines.append(f'|{result["market_data_cutoff"]}|{result["pattern_stage"]}|{m.get("no_new_low_sessions","—")}|'
            f'{distance if distance is not None else "—"}|{",".join(result["search_selected"]) or "無"}|{result["trade_conclusion"]}|{result["status"]}|')
        report = artifacts[result["checkpoint"] + "_report.json"]
        bundle = artifacts[result["checkpoint"] + "_input.json"]
        files[result["checkpoint"] + "_report.html"] = render_html(report, bundle)
    for result in results:
        lines.extend(["", "## " + result["checkpoint"], "", "規則缺口：" + "、".join(result["pattern_reasons"]),
            "交易缺口：" + "、".join(result["trade_reasons"]), ""])
        for role, review in {**result["standard_reviews"], **result["diagnostic_reviews"]}.items():
            lines.extend([role + " / " + review["verdict"] + "：" + review["summary"],
                "風險：" + "；".join(review["risks"]), "缺口：" + "；".join(review["missing_data"]), ""])
    markdown = "\n".join(lines)
    files["comparison.md"] = markdown
    files["comparison.html"] = '<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>Advantest 真實案例</title><style>body{font:16px/1.7 system-ui;max-width:1200px;margin:30px auto;padding:20px}pre{white-space:pre-wrap}</style><h1>Advantest 真實行情前綴與 AI 診斷</h1><pre>' + escape(markdown) + '</pre></html>'
    publish_run(target, files)
    return comparison, target


def replay_real_case(folder):
    folder = Path(folder)
    read = lambda name: strict_json_loads((folder/name).read_text(encoding="utf-8"))
    manifest = read("manifest.json")
    for name, expected in manifest["artifact_digests"].items():
        if not re.fullmatch(r"[A-Za-z0-9_]+\.json", name) or digest(read(name)) != expected:
            raise DataError("REAL_CASE_REPLAY_ARTIFACT_MISMATCH")
    case, source, documents = read("case.json"), read("source_bundle.json"), read("official_documents.json")
    comparison = read("comparison.json")
    checkpoints = {c["id"]: c for c in checkpoint_definitions(case, source)}
    for result in comparison["results"]:
        prefix = result["checkpoint"]
        bundle, original = read(prefix+"_input.json"), read(prefix+"_report.json")
        expected = build_real_bundle(source, case, checkpoints[prefix], documents=documents,
            registry=bundle["pattern_registry"], communication_policy=bundle["communication_policy"], evaluated_at=bundle["as_of"])
        if {k: v for k, v in bundle.items() if k != "discovery"} != expected:
            raise DataError("REAL_CASE_REPLAY_INPUT_MISMATCH")
        replayed = run(bundle, rules=Rules(**original["rules"]), reviews=original.get("reviews", []))
        for key in ("snapshot_id", "securities", "agent_packets", "pattern_opportunity_queue"):
            if replayed[key] != original[key]:
                raise DataError("REAL_CASE_REPLAY_RULE_OR_DECISION_MISMATCH")
    return {"status": "MATCH", "checkpoints": len(comparison["results"]), "network_calls": 0, "model_calls": 0,
        "historical_point_in_time_verified": False}
