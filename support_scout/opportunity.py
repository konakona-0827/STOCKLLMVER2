"""Conditional active-manual opportunity assessment, separate from formal gates."""
from copy import deepcopy
from pathlib import Path
import math

from .contracts import DataError, digest, strict_json_loads

DEFAULT_POLICY = Path(__file__).resolve().parent.parent / "config" / "opportunity.json"
ACTIONS = {"PROBE_BUY_REVIEW", "WAIT", "AVOID"}
STAGES = {"SUPPORT_HELD", "SUPPORT_RECLAIMED"}
LABELS = {"PROBE_BUY_REVIEW": "積極：可評估條件式試單", "WAIT": "機會評估：等待", "AVOID": "機會評估：暫不進場"}
DERIVED = {"trade_decision", "manual_action", "opportunity_decision", "execution_plan"}


def load_policy(value=None):
    value = deepcopy(value) if value is not None else strict_json_loads(DEFAULT_POLICY.read_text(encoding="utf-8"))
    fields = {"schema_version", "version", "enabled", "mode", "pattern_ids", "max_distance_fraction", "max_risk_fraction",
        "minimum_net_rr", "target_lookback_sessions", "resistance_pivot_left", "resistance_pivot_right",
        "entry_cost_fraction", "exit_cost_fraction", "gap_buffer_fraction", "agent_guidance"}
    if (not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["mode"] != "ACTIVE_MANUAL" or type(value["enabled"]) is not bool
            or not isinstance(value["version"], str) or not value["version"].strip()
            or value["pattern_ids"] != ["H-002"] or not isinstance(value["agent_guidance"], str) or not value["agent_guidance"].strip()):
        raise DataError("INVALID_OPPORTUNITY_POLICY")
    for key in ("max_distance_fraction", "max_risk_fraction", "entry_cost_fraction", "exit_cost_fraction", "gap_buffer_fraction"):
        v = value[key]
        if type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v < 1 or (key.startswith("max_") and v == 0):
            raise DataError("INVALID_OPPORTUNITY_LIMIT:" + key)
    if (type(value["minimum_net_rr"]) not in (int, float) or not math.isfinite(value["minimum_net_rr"])
            or not 1 <= value["minimum_net_rr"] <= 10 or type(value["target_lookback_sessions"]) is not int
            or not 5 <= value["target_lookback_sessions"] <= 100):
        raise DataError("INVALID_OPPORTUNITY_RISK_PARAMETERS")
    for key in ("resistance_pivot_left", "resistance_pivot_right"):
        if type(value[key]) is not int or not 1 <= value[key] <= 5:
            raise DataError("INVALID_OPPORTUNITY_RESISTANCE_PARAMETER")
    return value


def active_candidate(core):
    policy = core.get("opportunity_policy")
    return bool(policy and load_policy(policy)["enabled"] and not core.get("context_reject")
        and core.get("technical", {}).get("state") != "INSUFFICIENT_DATA"
        and any(p.get("pattern_id") in policy["pattern_ids"] and p.get("research_candidate") is True
            and p.get("stage") in STAGES for p in core.get("patterns", [])))


def validate_view(value, core, refs):
    if (not isinstance(value, dict) or set(value) != {"action", "pattern_id", "reason"}
            or value["action"] not in ACTIONS or value["pattern_id"] not in core["opportunity_policy"]["pattern_ids"]
            or not isinstance(value["reason"], str) or not 1 <= len(value["reason"].strip()) <= 240):
        raise DataError("INVALID_OPPORTUNITY_VIEW")
    pattern = next((p for p in core.get("patterns", []) if p["pattern_id"] == value["pattern_id"]), None)
    if value["action"] == "PROBE_BUY_REVIEW" and (not active_candidate(core) or pattern is None
            or not set(pattern["required_evidence_ids"]) <= set(refs)):
        raise DataError("UNSUPPORTED_OPPORTUNITY_VIEW")


def _reference_plan(core, pattern, policy):
    support = pattern.get("support") or {}
    entry, stop = core["technical"].get("latest_close"), support.get("lower")
    numeric = lambda x: type(x) in (int, float) and math.isfinite(x) and x > 0
    if not numeric(entry) or not numeric(stop) or stop >= entry:
        return None
    anchor = support.get("anchor_session")
    previous = [b for b in core["visible_series"]["bars"] if anchor and b["session"] < anchor][-policy["target_lookback_sessions"]:]
    if len(previous) < 5:
        return None
    left, right = policy["resistance_pivot_left"], policy["resistance_pivot_right"]
    levels = [{"session": bar["session"], "price": bar["high"]} for i, bar in enumerate(previous)
        if i >= left and i + right < len(previous) and bar["high"] > entry
        and all(bar["high"] > other["high"] for other in previous[i-left:i] + previous[i+1:i+right+1])]
    target = min(x["price"] for x in levels) if levels else max(b["high"] for b in previous[-5:])
    nearby_supply = [{"session": b["session"], "price": b["high"]}
        for b in core["visible_series"]["bars"] if anchor and b["session"] >= anchor and entry < b["high"] < target]
    friction = entry * (policy["entry_cost_fraction"] + policy["exit_cost_fraction"] + policy["gap_buffer_fraction"])
    risk, reward = entry - stop + friction, target - entry - friction
    return {"channel": pattern["pattern_id"], "entry_reference": entry, "invalidation_reference": stop,
        "target_reference": target, "risk_fraction": risk / entry, "net_reward_risk": reward / risk,
        "price_basis": core["visible_series"].get("price_basis"), "anchor_session": anchor,
        "target_basis": "NEAREST_CONFIRMED_PRE_ANCHOR_PIVOT_OR_RECENT_FIVE_HIGH_NOT_FORECAST",
        "resistance_candidates": levels, "target_window_end": previous[-1]["session"],
        "nearby_supply": nearby_supply,
        "target_limitation": "錨日及近期高點仍可能形成更近供給，主要pivot不是唯一阻力；兩方須評估首次反彈被阻擋的風險。",
        "target_window_count": len(previous),
        "validity": "RECHECK_NEXT_SESSION_PRICE_GAP_AND_SOURCE",
        "invalidation_basis": support.get("invalidation_basis", "ANY_LATER_DAILY_LOW_BELOW_FIXED_LOWER_REQUIRES_REJECTION"),
        "meaning": "參考收盤不是限價單；後續日線最低價穿過本型態下緣即失效，並非即時或保證成交的停損單。沒有訂單或部位數量。"}


def attach_opportunities(report, bundle):
    policy = report.get("opportunity_policy")
    if not policy or not load_policy(policy)["enabled"]:
        return report
    from .agents import validate_reviews
    from .decision import _packet_for
    projection = [{k: v for k, v in row.items() if k not in DERIVED} for row in report["securities"]]
    integrity = digest([r.get("packet_id", r) for r in projection]) == report["snapshot_id"]
    integrity = integrity and bool(bundle.get("discovery_required")) == bool(report.get("discovery_required"))
    if report.get("discovery_required"):
        integrity = integrity and digest(bundle.get("source_universe")) == report.get("source_universe_hash")
    integrity = integrity and (bundle.get("synthetic") is True) == (report.get("synthetic") is True)
    counts = dict.fromkeys(sorted(ACTIONS), 0)
    for item in report["securities"]:
        action, reasons, views, plan = "WAIT", [], {}, None
        patterns = [p for p in item.get("patterns", []) if p["pattern_id"] in policy["pattern_ids"]]
        pattern = next((p for p in patterns if p.get("research_candidate") and p.get("stage") in STAGES), patterns[0] if patterns else None)
        candidate = bool(pattern and pattern.get("research_candidate") and pattern.get("stage") in STAGES)
        packet = _packet_for(report, item)
        if not integrity or policy != bundle.get("opportunity_policy") or (candidate and packet is None):
            reasons.append("OPPORTUNITY_SNAPSHOT_INTEGRITY_FAILED")
        elif item.get("data_status") == "REJECTED":
            action, reasons = "AVOID", ["PRICE_DATA_REJECTED"]
        elif packet and packet["core"].get("context_reject"):
            action, reasons = "AVOID", ["COMPANY_THESIS_BROKEN"]
        elif not candidate:
            action = "AVOID" if pattern is None or pattern.get("stage") in {"NO_MATCH", "INVALIDATED", "EXTENDED", "EXPIRED", "CONTRADICTED"} else "WAIT"
            reasons = ["NO_ACTIVE_H002_SETUP", *(pattern.get("reason_codes", []) if pattern else [])]
        else:
            core = packet["core"]
            plan = _reference_plan(core, pattern, policy)
            distance = pattern.get("metrics", {}).get("distance_from_support_fraction")
            raw = item.get("agent_reviews", {})
            accepted, errors = validate_reviews(packet, list(raw.values())) if isinstance(raw, dict) else ({}, ["INVALID_REVIEWS"])
            views = {role: deepcopy(r["opportunity_view"]) for role, r in accepted.items() if r.get("opportunity_view")}
            search_ok = not report.get("discovery_required")
            if report.get("discovery_required"):
                try:
                    from .discovery import validate_search_result
                    search = report.get("discovery", {}).get("search")
                    search_ok = item["symbol"] in {r["symbol"] for r in validate_search_result(search, report)}
                except (DataError, TypeError, KeyError, ValueError):
                    search_ok = False
            if report.get("discovery_required") is not True or not search_ok:
                reasons.append("ACTIVE_SEARCH_SUBMISSION_REQUIRED")
            ledger = report.get("ledger", {})
            if ledger.get("errors") or ledger.get("status") == "DEGRADED" or any(
                    row.get("symbol") == item["symbol"] and (row.get("change") in {"STALE_IGNORED", "REVISION_REVIEW"}
                        or row.get("revision_review")) for row in ledger.get("rows", [])):
                reasons.append("ACTIVE_LEDGER_DATA_REQUIRES_REVIEW")
            if type(distance) not in (int, float) or not math.isfinite(distance) or distance > policy["max_distance_fraction"]:
                action, reasons = "AVOID", ["ACTIVE_PRICE_EXTENDED_OR_UNKNOWN"]
            elif plan is None:
                reasons.append("ACTIVE_REFERENCE_PLAN_UNAVAILABLE")
            elif plan["risk_fraction"] > policy["max_risk_fraction"] or plan["net_reward_risk"] < policy["minimum_net_rr"]:
                action, reasons = "AVOID", ["ACTIVE_RISK_OR_NET_RR_FAILED"]
            elif any(v["action"] == "AVOID" for v in views.values()):
                action, reasons = "AVOID", ["ACTIVE_AGENT_VETO"]
            elif errors or set(views) != {"researcher", "reviewer"}:
                reasons.append("ACTIVE_DUAL_REVIEW_INCOMPLETE")
            elif any(r.get("public_information", {}).get("gaps") for r in accepted.values()):
                reasons.append("ACTIVE_PUBLIC_SOURCE_GAPS_REQUIRE_REVIEW")
            elif any(v["action"] != "PROBE_BUY_REVIEW" for v in views.values()):
                reasons.append("ACTIVE_AGENTS_WAIT_OR_DISAGREE")
            elif not reasons:
                action = "PROBE_BUY_REVIEW"
                reasons = ["SEARCH_AND_BOTH_AGENTS_SUPPORT_CONDITIONAL_PROBE"]
        core = packet["core"] if packet else {}
        simulation = report.get("synthetic") is not False or core.get("analysis_context", {}).get("mode", "").startswith("RETROSPECTIVE")
        checks = list(dict.fromkeys([*item.get("context_blockers", []), "MANUALLY_VERIFY_NEXT_SESSION_PRICE_AND_GAP",
            "VERIFY_CORPORATE_ACTIONS_AND_BROKER_ELIGIBILITY", "USER_MUST_CHOOSE_AFFORDABLE_RISK_AND_CONFIRM_FILL"]))
        result = {"action": action, "label": LABELS[action], "candidate": candidate,
            "pattern_id": pattern["pattern_id"] if pattern else None, "policy_version": policy["version"],
            "reason_codes": reasons, "agent_views": views, "reference_plan": plan, "manual_checks": checks,
            "scope": "SIMULATION_ONLY" if simulation else "CONDITIONAL_MANUAL_REVIEW",
            "formal_qualification": item.get("trade_decision", {}).get("conclusion"), "is_trade_approval": False,
            "execution_enabled": False, "meaning": "積極機會評估；未核實資料仍列出，須人工核對並自行決定，不代表正式交易資格或訂單。"}
        item["opportunity_decision"] = result
        counts[action] += 1
    report["opportunity_summary"] = {"counts": counts, "policy_version": policy["version"], "execution_enabled": False}
    return report
