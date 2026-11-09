"""Replay saved SEARCH coverage; counterfactual orders never invent AI reviews."""
from collections import Counter
from copy import deepcopy
from datetime import datetime
from html import escape
from pathlib import Path
from uuid import uuid4

from .contracts import DataError, TOKYO, digest, strict_json_loads
from .decision_lab import replay_lab
from .search_priority import LEGACY, SUPPORT_EVIDENCE, rank_candidates, support_evidence
from .settings import ROOT
from .storage import atomic_json

PLAN = {"version": "search-coverage-v1-20261105", "baseline": LEGACY, "variant": SUPPORT_EVIDENCE,
    "scope": "BASELINE_PANELS_ALL_CASES_SAVED_TRANSMITTED_POOL",
    "capacity": "SAVED_CAP_AND_SAVED_CAP_PLUS_ONE_SENSITIVITY_ONLY",
    "proximity": "EACH_FROZEN_SUPPORT_MODEL_LIMIT_NO_FITTED_THRESHOLDS",
    "selection": "DETERMINISTIC_TOP_K_PROXY_NOT_A_NEW_SEARCH_MODEL_ANSWER",
    "score_holding_sessions": 5, "production_threshold_changes": False,
    "future_prices_used_for_selection": False, "counterfactual_reviews": "UNKNOWN_NOT_REUSED_OR_FABRICATED"}


def read_json(path):
    return strict_json_loads(Path(path).read_text(encoding="utf-8"))


def freeze_selections(panels):
    """Only packets go in here: no outcome, price path, action or reviews."""
    decisions = []
    for panel, report in panels:
        search = report.get("discovery", {}).get("search") or {}
        packet = search.get("search_packet")
        if not packet:
            raise DataError("SEARCH_LAB_SAVED_PACKET_REQUIRED")
        from .discovery import verify_search_packet
        verify_search_packet(packet)
        core = packet["core"]
        cs = core["candidates"]
        baseline = rank_candidates(cs, version=LEGACY, cutoff=core["as_of"])
        if [c["candidate_id"] for c in baseline] != [c["candidate_id"] for c in cs]:
            raise DataError("SEARCH_LAB_LEGACY_ORDER_MISMATCH")
        improved = rank_candidates(cs, version=SUPPORT_EVIDENCE, cutoff=core["as_of"])
        cap = core["config"]["max_selected"]
        decisions.append({"panel_id": panel["id"], "session": panel["session"],
            "packet_digest": digest(packet), "max_selected": cap, "max_candidates": core["config"]["max_candidates"],
            "eligible_count": len(core["eligible_candidate_ids"]), "transmitted_count": len(cs),
            "pool_clipped": len(core["eligible_candidate_ids"]) > len(cs),
            "orders": {LEGACY: [c["symbol"] for c in baseline], SUPPORT_EVIDENCE: [c["symbol"] for c in improved]},
            "support_evidence": {c["symbol"]: support_evidence(c, core["as_of"]) for c in cs},
            "selections": {"legacy_top_k": [c["symbol"] for c in baseline[:cap]],
                "support_top_k": [c["symbol"] for c in improved[:cap]],
                "support_top_k_plus_one": [c["symbol"] for c in improved[:cap+1]]}})
    return decisions


def compare_saved(panels, saved_rows, decisions):
    rows = []
    reports = {p["id"]: r for p, r in panels}
    scored = {(r["panel_id"], r["symbol"]): r for r in saved_rows
        if r["profile_id"] == "baseline" and r["outcome"]["holding_sessions"] == 5}
    for d in decisions:
        report = reports[d["panel_id"]]
        search = report["discovery"]["search"]
        packet = search["search_packet"]["core"]
        cs = {c["symbol"]: c for c in packet["candidates"]}
        selected = {s["symbol"]: s for s in search["selection"]}
        deferred = set(report.get("desk", {}).get("deferred_symbols", []))
        for item in report["securities"]:
            symbol = item["symbol"]
            c = cs.get(symbol)
            row = scored[(d["panel_id"], symbol)]
            pid = item.get("packet_id")
            in_eligible = pid in packet["eligible_candidate_ids"]
            full = len(selected) >= d["max_selected"]
            reviewed = row.get("research_status") == "REVIEWED"
            if reviewed:
                stage = "REVIEWED"
            elif symbol in selected:
                stage = "DESK_CAPACITY_DEFERRED" if symbol in deferred else "SELECTED_REVIEW_INCOMPLETE"
            elif not c:
                stage = "POOL_TRUNCATED" if in_eligible else "NOT_ELIGIBLE_POOL"
            elif search["status"] == "FAILED":
                stage = "SEARCH_FAILED"
            else:
                stage = "SEARCH_NOT_SELECTED_CAP_FULL" if full else "SEARCH_NOT_SELECTED_SPARE_CAPACITY"
            rows.append({"panel_id": d["panel_id"], "session": d["session"], "symbol": symbol,
                "in_eligible_pool": in_eligible, "in_search_packet": bool(c),
                "legacy_pre_rank": d["orders"][LEGACY].index(symbol)+1 if c else None,
                "support_pre_rank": d["orders"][SUPPORT_EVIDENCE].index(symbol)+1 if c else None,
                "search_selected": symbol in selected, "research_status": row["research_status"],
                "selected_symbols": list(selected), "selection_cap": d["max_selected"], "cap_full": full,
                "stage": stage, "unselected_model_rank": None,
                "attribution_limit": "FULL_CAP_AND_MODEL_CHOICE_COEXIST_NO_UNSELECTED_MODEL_RANK_SAVED" if c and symbol not in selected and full else None,
                "support_grade": d["support_evidence"].get(symbol, {}).get("grade", 0),
                "variants": {k: symbol in v for k, v in d["selections"].items()},
                "rule_outcome": deepcopy(row["primary_comparator"]),
                "missed_rule_target": row["action"] != "PROBE_BUY_REVIEW" and row["primary_comparator"]["status"] == "TARGET",
                "counterfactual_research": "NOT_RUN_NO_BUY_OR_RETURN_INFERENCE"})
    summary = []
    for name in ("saved_search", "legacy_top_k", "support_top_k", "support_top_k_plus_one"):
        chosen = lambda r: r["search_selected"] if name == "saved_search" else r["variants"][name]
        selected_rows = [r for r in rows if chosen(r)]
        supported = [r for r in rows if r["support_grade"] > 0]
        targets = [r for r in rows if r["rule_outcome"]["status"] == "TARGET"]
        missed = [r for r in rows if r["missed_rule_target"] and not r["search_selected"]]
        summary.append({"policy": name, "cases": len(rows), "research_slots": len(selected_rows),
            "model_calls": 0, "potential_review_calls": 2*len(selected_rows),
            "support_evidence_cases": len(supported), "support_evidence_covered": sum(chosen(r) for r in supported),
            "rule_target_cases": len(targets), "rule_targets_covered": sum(chosen(r) for r in targets),
            "original_unselected_rule_targets": len(missed), "original_unselected_rule_targets_covered": sum(chosen(r) for r in missed),
            "rule_outcomes_among_selected": dict(Counter(r["rule_outcome"]["status"] for r in selected_rows)),
            "interpretation": "RESEARCH_COVERAGE_NOT_AI_BUY_SUCCESS_OR_VALIDATED_EDGE"})
    return {"rows": rows, "summary": summary,
        "missed_cases": [r for r in rows if r["missed_rule_target"] and not r["search_selected"]],
        "reviewed_wait_targets": [r for r in rows if r["missed_rule_target"] and r["search_selected"]]}


def load_panels(directory):
    # Full source/prefix/decision integrity is checked before accepting this study.
    replay_lab(directory)
    comparison = read_json(directory/"comparison.json")
    if not comparison["agents"]:
        raise DataError("SEARCH_LAB_AGENT_STUDY_REQUIRED")
    panels = [(p, read_json(directory/(p["id"]+"_report.json"))) for p in comparison["panels"] if p["profile_id"] == "baseline"]
    return comparison, panels


def run_comparison(directory, *, output_root=None):
    directory = Path(directory).resolve()
    comparison, panels = load_panels(directory)
    selections = freeze_selections(panels)
    target = Path(output_root or ROOT/"output"/"search-lab")/(datetime.now(TOKYO).strftime("%Y%m%dT%H%M%S")+"-"+uuid4().hex[:10])
    frozen = {"plan": PLAN, "lab_directory": str(directory), "lab_comparison_digest": digest(comparison), "selections": selections}
    # Persist every counterfactual selection before attaching any outcome labels.
    atomic_json(target/"frozen.json", frozen)
    result = {"version": PLAN["version"], "frozen_digest": digest(frozen),
        **compare_saved(panels, comparison["rows"], selections), "network_calls": 0, "model_calls": 0,
        "formal_thresholds_changed": False, "empirical_edge_validated": False, "execution_enabled": False}
    atomic_json(target/"comparison.json", result)
    (target/"comparison.html").write_text(render_comparison(result), encoding="utf-8")
    return result, target


def replay_comparison(directory, *, lab_directory=None):
    directory = Path(directory)
    frozen, result = read_json(directory/"frozen.json"), read_json(directory/"comparison.json")
    if frozen["plan"] != PLAN or digest(frozen) != result["frozen_digest"]:
        raise DataError("SEARCH_LAB_FROZEN_INTEGRITY_FAILED")
    comparison, panels = load_panels(Path(lab_directory or frozen["lab_directory"]))
    if digest(comparison) != frozen["lab_comparison_digest"] or freeze_selections(panels) != frozen["selections"]:
        raise DataError("SEARCH_LAB_SELECTION_REPLAY_MISMATCH")
    expected = {"version": PLAN["version"], "frozen_digest": digest(frozen),
        **compare_saved(panels, comparison["rows"], frozen["selections"]), "network_calls": 0, "model_calls": 0,
        "formal_thresholds_changed": False, "empirical_edge_validated": False, "execution_enabled": False}
    if result != expected:
        raise DataError("SEARCH_LAB_COMPARISON_REPLAY_MISMATCH")
    return {"status": "MATCH", "panels": len(panels), "cases": len(result["rows"]), "network_calls": 0, "model_calls": 0}


def render_comparison(result):
    names = {"saved_search": "保存的實際SEARCH", "legacy_top_k": "原排序前K（代理比較）",
        "support_top_k": "支撐證據排序前K（代理比較）", "support_top_k_plus_one": "支撐排序K+1（名額敏感度）"}
    summary = ''.join(f'<tr><td>{names[s["policy"]]}</td><td>{s["research_slots"]}</td>'
        f'<td>{s["support_evidence_covered"]}/{s["support_evidence_cases"]}</td>'
        f'<td>{s["rule_targets_covered"]}/{s["rule_target_cases"]}</td>'
        f'<td>{s["original_unselected_rule_targets_covered"]}/{s["original_unselected_rule_targets"]}</td></tr>' for s in result["summary"])
    misses = ''.join(f'<tr><td>{r["session"]}</td><td>{r["symbol"]}</td><td>{r["legacy_pre_rank"]} → {r["support_pre_rank"]}</td>'
        f'<td>{escape(r["stage"])}</td><td>{escape(", ".join(r["selected_symbols"]))}</td></tr>' for r in result["missed_cases"])
    return ('<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>SEARCH離線覆蓋比較</title>'
        '<style>body{font:16px sans-serif;max-width:1200px;margin:40px auto;line-height:1.7}td,th{border:1px solid #aaa;padding:10px}table{border-collapse:collapse}</style>'
        '<h1>SEARCH候選排序與研究覆蓋</h1><p>僅使用已保存資料；模型與網路呼叫皆0，正式門檻不變。</p>'
        '<p>先凍結所有截點的選擇，再附加事後標籤；前K是確定性排序代理，沒有重跑SEARCH或未研究標的的研究／反證。覆蓋不等於買訊、成交、獲利或未來優勢。K+1只顯示多一組研究的成本與敏感度，不自動提高正式名額。</p>'
        '<table><tr><th>方式</th><th>研究名額</th><th>近支撐有證據覆蓋</th><th>固定規則達標覆蓋</th><th>原5漏選覆蓋</th></tr>'+summary+'</table>'
        '<h2>原漏選逐筆歸因</h2><p>名額已滿與SEARCH取捨同時存在；沒有保存未選者完整模型排名，無法判定若增加名額就必定選中。</p>'
        '<table><tr><th>截點</th><th>代號</th><th>原 → 新提示排名</th><th>階段</th><th>實際交接</th></tr>'+misses+'</table></html>')
