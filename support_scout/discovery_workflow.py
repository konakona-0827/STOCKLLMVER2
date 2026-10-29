"""Discovery -> existing independent research/review -> immutable observation artifacts."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from html import escape
from functools import wraps
import json
from pathlib import Path
from uuid import uuid4

from .contracts import DataError, Rules, digest, strict_json_loads
from .demo import demo_bundle
from .desk import DeskConfig, run_desk
from .discovery import (DiscoveryConfig, OpenAIDiscoveryProvider, ReplayDiscoveryProvider,
                        ScoutTask, _safe_error, load_plan, run_discovery)
from .observation import observe
from .pipeline import run
from .providers import OfflineProvider, OpenAIProvider
from .settings import DEFAULT_SETTINGS, ROOT, load_settings, openai_config
from .storage import atomic_json, publish_run, publish_latest

DEFAULT_PLAN = ROOT / "config" / "discovery.json"
JST = timezone(timedelta(hours=9))


def demo_inputs():
    """Small explicit fixtures; these scenario codes are not real-stock leads."""
    base = demo_bundle()
    bundles, rows = {}, []
    for item in base["securities"][:5]:
        bundle = deepcopy(base)
        bundle["securities"] = [deepcopy(item)]
        bundles[item["symbol"]] = bundle
        last = item["bars"][-1]
        rows.append({"symbol": item["symbol"], "name": item["name"], "industry": "DEMO",
            "session": last["session"], "close": last["close"], "volume_shares": last["volume"],
            "turnover_twd": last["turnover_twd"], "change_pct": 0.0})
    market = {"schema_version": 1, "market": "TWSE", "synthetic": True, "as_of": base["as_of"],
        "session": base["calendar"]["sessions"][-1], "securities": rows,
        "status": "COMPLETE", "sources": [], "coverage": {"eligible_count": len(rows)},
        "limitations": ["SYNTHETIC_SCENARIOS_NOT_REAL_STOCK_RECOMMENDATIONS"]}
    market["catalog_id"] = digest(market)
    return market, bundles


class OfflineDiscoveryProvider:
    """Scripted tool-contract demonstration, explicitly not autonomous model discovery."""
    mode = "offline_simulation"
    model = None

    def respond(self, messages, task, config, timeout):
        outputs = [m for m in messages if m.get("type") == "function_call_output"]
        count = len(outputs)
        if count == 0:
            name, args = "market_overview", {}
        elif count == 1:
            name, args = "search_market", {"industry": None, "query": None, "sort": "symbol",
                "min_turnover_twd": 0, "offset": 0, "limit": 5}
        elif count == 2:
            rows = strict_json_loads(outputs[-1]["output"]).get("securities", [])
            if rows:
                name, args = "inspect_support", {"symbol": rows[0]["symbol"]}
            else:
                return self._final([])
        else:
            value = strict_json_loads(outputs[-1]["output"])
            leads = [] if value.get("status") != "OK" else [{"symbol": value["symbol"],
                "history_evidence_id": value["history_evidence_id"],
                "rationale": "離線示範：依市場工具回應選擇個股，已實際讀取合成日線；等待雙方研究。",
                "missing_data": ["真實市場探索及模型研究"]}]
            return self._final(leads)
        return {"status": "completed", "output": [{"type": "function_call", "name": name,
            "arguments": json.dumps(args, ensure_ascii=False), "call_id": "demo-" + str(count)}]}

    @staticmethod
    def _final(leads):
        return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text",
            "text": json.dumps({"leads": leads, "search_summary": "合成資料工具契約示範；未呼叫真實模型。",
                "unexplored": ["其餘市場與未讀日線"]}, ensure_ascii=False)}]}]}


def render_discovery(record):
    c, u = record["coverage"], record["usage"]
    lines = ["# 尋標 Desk" + ("（合成離線示範）" if record["mode"] == "offline_simulation" else ""), "",
        "狀態：" + record["status"] + "；市場資料：" + str(record.get("market_status")), "",
        f"市場可查 {c['catalog_symbols']} 檔；看過 {c['viewed_symbols']} 檔；讀日線 {c['inspected_histories']} 檔；提出 {c['nominated_symbols']} 條線索。",
        "有限探索，不代表整個市場沒有其他機會。尚未完成研究／反證的線索不能當作決策。", "",
        f"尋標請求 {u['request_count']} 次；已知輸入 {u['input_tokens']} / 輸出 {u['output_tokens']} token；未知用量 {u['unknown_usage_requests']} 次；工具錯誤 {record['tool_error_count']} 次。", "",
        "| 線索 | 股票代號 | 提名角色 | 狀態 |", "|---|---|---|---|"]
    for lead in record["leads"]:
        lines.append("| " + " | ".join([lead["lead_id"], lead["symbol"],
            ", ".join(n["role"] for n in lead["nominations"]), lead["status"]]) + " |")
    for scout in record["scouts"]:
        lines.extend(["", f"{scout['role']}：{scout['status']}；錯誤：{scout['error'] or '無'}",
                      scout["search_summary"], "尚未探索：" + "；".join(scout["unexplored"])])
    lines.extend(["", "僅供觀察；不下單，未啟用帳戶損益停工。", ""])
    return "\n".join(lines)


def _artifacts(record, repository, tasks, config):
    return {"discovery.json": record, "market.json": repository.market,
        "discovery_histories.json": repository.bundles,
        "discovery_plan.json": {"version": record["protocol"], "scouts": [asdict(t) for t in tasks],
                                 "limits": asdict(config)}}


def _track_attempts(function):
    @wraps(function)
    def tracked(**kwargs):
        output_root = Path(kwargs.get("output_root") or ROOT / "output" / "discovery")
        attempt_id = datetime.now(JST).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:10]
        try:
            result = function(**kwargs)
            record, report, target, code = result
            atomic_json(output_root / "latest_attempt.json", {"attempt_id": attempt_id,
                "run_id": target.name, "status": record["status"], "exit_code": code,
                "report": str((target / "report.html").resolve()), "occurred_at": datetime.now(JST).isoformat()})
            return result
        except Exception as exc:
            error = _safe_error(exc)
            failure = {"attempt_id": attempt_id, "status": "FAILED", "error": error,
                       "occurred_at": datetime.now(JST).isoformat()}
            atomic_json(output_root / "failures" / (attempt_id + ".json"), failure)
            atomic_json(output_root / "latest_attempt.json", failure)
            raise DataError("DISCOVERY_FAILED:" + error) from None
    return tracked


@_track_attempts
def discover(*, plan_path=DEFAULT_PLAN, settings_path=DEFAULT_SETTINGS, output_root=None, data_root=None,
             model=None, demo=False, review=True, max_packets=5, max_calls=10,
             max_leads=None, max_history_symbols=None,
             market=None, history_loader=None, providers=None, researcher=None, reviewer=None,
             progress=None):
    tasks, config = load_plan(plan_path)
    overrides = {key: value for key, value in (("max_leads", max_leads),
                 ("max_history_symbols", max_history_symbols)) if value is not None}
    config = replace(config, **overrides)
    rules = Rules()
    output_root = Path(output_root or ROOT / "output" / "discovery")
    data_root = Path(data_root or ROOT / "local_data" / "discovery")
    # Reserve a separate, complete-pair budget before any exploration is started.
    DeskConfig(max_packets, max_calls)
    ai = None
    if demo:
        if market is None:
            market, histories = demo_inputs()
            history_loader = lambda symbol: deepcopy(histories[symbol])
        providers = providers or {t.role: OfflineDiscoveryProvider() for t in tasks}
        researcher, reviewer = researcher or OfflineProvider(), reviewer or OfflineProvider()
    else:
        if providers is None or (review and (researcher is None or reviewer is None)):
            ai = openai_config(load_settings(Path(settings_path)), model=model)
            if not ai["ready"]:
                raise DataError("DISCOVERY_OPENAI_CONFIGURATION_REQUIRED")
        providers = providers or {t.role: OpenAIDiscoveryProvider(ai["model"]) for t in tasks}
        if review:
            researcher = researcher or OpenAIProvider(ai["model"])
            reviewer = reviewer or OpenAIProvider(ai["reviewer_model"])
        if market is None or history_loader is None:
            from .market import build_market_snapshot
            from .twse import TwseClient, build_snapshot
            client = TwseClient(data_root / "twse-cache")
            market = market or build_market_snapshot(data_root / "twse-cache", client=client)
            history_loader = history_loader or (lambda symbol: build_snapshot([symbol], data_root / "twse-cache", client=client, progress=progress))
    if not demo and market.get("synthetic") is not False:
        raise DataError("LIVE_DISCOVERY_REQUIRES_REAL_MARKET")
    record, repository = run_discovery(market, history_loader, providers=providers, tasks=tasks, config=config, rules=rules)
    values = _artifacts(record, repository, tasks, config)
    symbols = [lead["symbol"] for lead in record["leads"]]
    report = None
    if symbols:
        bundle = repository.combine(symbols)
        if not demo:
            report, target, code = observe(bundle=bundle, agents=review, rules=rules,
                researcher=researcher, reviewer=reviewer, settings_path=settings_path,
                output_root=output_root, db_path=data_root / "observations.sqlite3",
                max_packets=max_packets, max_calls=max_calls,
                discovery_context={"audit": record, "artifacts": values})
            return record, report, target, code
        report = run_desk(bundle, researcher=researcher, reviewer=reviewer,
                          config=DeskConfig(max_packets, max_calls), rules=rules) if review else run(bundle, rules=rules)
        values.update({"input.json": bundle, "report.json": report, "rules.json": asdict(rules)})
    run_id = datetime.now(JST).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:10]
    target = output_root / run_id
    code = 3 if record["status"] == "DEGRADED" else 0
    manifest = {"run_id": run_id, "mode": record["mode"], "catalog_id": market["catalog_id"],
        "status": record["status"], "as_of": market["as_of"], "exit_code": code,
        "artifact_digests": {name: digest(value) for name, value in values.items()},
        "review_requested": review, "ledger_written": False, "execution_enabled": False}
    values["manifest.json"] = manifest
    files = {name: json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n" for name, value in values.items()}
    markdown = render_discovery(record)
    if report is not None:
        from .pipeline import render_markdown
        markdown += "\n" + render_markdown(report)
    files.update({"report.md": markdown, "report.html": '<!doctype html><meta charset="utf-8"><title>尋標 Desk</title><pre>' + escape(markdown) + '</pre>'})
    publish_run(target, files)
    atomic_json(target / "completed.json", {"run_id": run_id, "ledger_written": False})
    # A distinct pointer prevents empty/discovery-only runs impersonating an observation report.
    publish_latest(output_root / "latest_discovery.json", {"run_id": run_id, "as_of": market["as_of"],
        "report": str((target / "report.html").resolve()), "exit_code": code})
    return record, report, target, code


def replay_discovery(folder):
    folder = Path(folder)
    read = lambda name: strict_json_loads((folder / name).read_text(encoding="utf-8"))
    market, saved, histories = read("market.json"), read("discovery.json"), read("discovery_histories.json")
    manifest = read("manifest.json")
    is_observation = manifest.get("ledger_written") is not False
    marker_name = "committed.json" if is_observation else "completed.json"
    if not (folder / marker_name).is_file():
        raise DataError("DISCOVERY_REPLAY_RUN_NOT_COMPLETED")
    marker = read(marker_name)
    if (marker.get("run_id") != manifest.get("run_id") or
            (is_observation and marker.get("ledger_committed") is not True) or
            (not is_observation and marker.get("ledger_written") is not False)):
        raise DataError("DISCOVERY_REPLAY_INVALID_COMPLETION_MARKER")
    hashes = manifest.get("discovery_artifact_digests") if is_observation else manifest.get("artifact_digests")
    required = {"market.json", "discovery.json", "discovery_histories.json", "discovery_plan.json"}
    allowed = required | {"input.json", "report.json", "rules.json"}
    if not isinstance(hashes, dict) or not required <= set(hashes) or not set(hashes) <= allowed:
        raise DataError("DISCOVERY_REPLAY_MANIFEST_DIGESTS_REQUIRED")
    for name, expected in hashes.items():
        if digest(read(name)) != expected:
            raise DataError("DISCOVERY_REPLAY_ARTIFACT_INTEGRITY_FAILED")
    tasks, config = load_plan(folder / "discovery_plan.json")
    providers = {s["role"]: ReplayDiscoveryProvider(s) for s in saved["scouts"]}
    replayed, repository = run_discovery(market, lambda symbol: deepcopy(histories[symbol]),
        providers=providers, tasks=tasks, config=config, rules=Rules(**saved["rules"]))
    project = lambda record: [(l["lead_id"], l["symbol"], l["history_evidence_id"], l["nominations"]) for l in record["leads"]]
    if project(saved) != project(replayed) or saved["coverage"] != replayed["coverage"]:
        raise DataError("DISCOVERY_REPLAY_RESULT_MISMATCH")
    if any(s["status"] == "ERROR" for s in saved["scouts"]):
        raise DataError("DISCOVERY_REPLAY_INCOMPLETE_OR_FAILED_SCOUT")
    decisions = None
    if (folder / "input.json").is_file():
        original = read("report.json")
        bundle = read("input.json")
        if digest(bundle) != digest(repository.combine([l["symbol"] for l in replayed["leads"]])):
            raise DataError("DISCOVERY_REPLAY_HANDOFF_INPUT_MISMATCH")
        if is_observation and (digest(bundle) != manifest.get("input_digest") or
                digest(original) != manifest.get("report_digest") or
                digest(original["rules"]) != manifest.get("rules_hash") or
                original["rules"] != manifest.get("rules") or
                original["rules"] != read("rules.json")):
            raise DataError("DISCOVERY_REPLAY_OBSERVATION_INTEGRITY_FAILED")
        result = run(bundle, rules=Rules(**original["rules"]),
                     as_of=original["as_of"], reviews=original.get("reviews", []))
        decisions = [(s["symbol"], s["verdict"]) for s in result["securities"]]
        core_fields = ("snapshot_id", "as_of", "rules", "securities", "technical_queue",
                       "reviewed_shortlist", "agent_packets", "unmatched_review_packets")
        if any(result[key] != original[key] for key in core_fields):
            raise DataError("DISCOVERY_REPLAY_REVIEW_MISMATCH")
        if is_observation and (result["snapshot_id"] != manifest.get("snapshot_id") or
                result["as_of"] != manifest.get("effective_as_of")):
            raise DataError("DISCOVERY_REPLAY_SNAPSHOT_MISMATCH")
    elif is_observation or replayed["leads"]:
        raise DataError("DISCOVERY_REPLAY_HANDOFF_INPUT_REQUIRED")
    return {"status": "MATCH", "catalog_id": market["catalog_id"], "leads": len(replayed["leads"]),
            "coverage": replayed["coverage"], "decisions": decisions, "network_calls": 0, "model_calls": 0}
