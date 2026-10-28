"""Deterministic, snapshot-bound decisions. This module has no execution capability.

The research verdict remains a separate field. A model cannot choose an entry,
move a frozen invalidation level, invent a target, or relax these checks.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math

from .agents import ROLES, decide, validate_reviews, verify_packet
from .contracts import DataError, digest, timestamp


@dataclass(frozen=True)
class DecisionPolicy:
    version: str = "jp-support-decision-v2-search-gate"
    minimum_net_rr: float = 2.0
    roundtrip_cost_bps: float = 20.0
    gap_buffer_atr: float = 0.25
    resistance_lookback_sessions: int = 20

    def __post_init__(self):
        for field in ("minimum_net_rr", "roundtrip_cost_bps", "gap_buffer_atr"):
            value = getattr(self, field)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise DataError("INVALID_DECISION_POLICY:" + field)
        if not 0 < self.minimum_net_rr <= 100 or self.roundtrip_cost_bps > 10_000 or self.gap_buffer_atr > 10:
            raise DataError("INCONSISTENT_DECISION_POLICY")
        if type(self.resistance_lookback_sessions) is not int or not 1 <= self.resistance_lookback_sessions <= 250:
            raise DataError("INVALID_RESISTANCE_LOOKBACK")
        if not isinstance(self.version, str) or not self.version.strip():
            raise DataError("DECISION_POLICY_VERSION_REQUIRED")


LABELS = {"CONSIDER": "可考慮交易", "WAIT": "等待確認", "NO_TRADE": "目前不交易"}
MESSAGES = {
    "SYNTHETIC_DATA": "合成情境只能驗證流程，不能產生真實交易候選。",
    "OBSERVATION_ONLY": "目前資料只供觀察；公司行動、公告與交易狀態待核實。",
    "DATA_REJECTED": "行情資料未通過完整性驗證。",
    "DATA_COLLECTION_DEGRADED": "本輪資料取得不完整。",
    "SUPPORT_INVALIDATED": "固定支撐已失效。",
    "NO_CURRENT_SETUP": "目前沒有可用的支撐型態，或型態已過期。",
    "SUPPORT_NOT_CONFIRMED": "尚未完成反彈、另日回測與後續收盤確認。",
    "NOT_NEAR_SUPPORT": "目前價格已離支撐過遠，不追價。",
    "TECHNICAL_GATE_BLOCKED": "技術條件尚未全部通過。",
    "CONTEXT_GATE_BLOCKED": "公司、事件或市場資料尚有阻擋項目。",
    "RESEARCH_REJECTED": "固定規則或研究審查已否決本情境。",
    "RESEARCH_NOT_APPROVED": "研究資格尚未通過。",
    "DUAL_REVIEW_REQUIRED": "需要同一快照的研究員與反證員各自完成審查。",
    "AGENT_VALIDATION_FAILED": "代理評語的身分、時點或證據驗證失敗。",
    "STALE_OR_UNMATCHED_REVIEWS": "存在無法對應目前快照的評語，不能沿用。",
    "SNAPSHOT_INTEGRITY_FAILED": "目前結果與原始快照不一致，需重新產生。",
    "REFERENCE_PLAN_UNAVAILABLE": "目前無法從可見資料建立完整的參考價位。",
    "NO_HISTORICAL_RESISTANCE": "支撐前的可見歷史不足，無法提出有依據的目標。",
    "TARGET_NOT_ABOVE_REFERENCE": "已知前高不高於參考入場價，缺乏上行空間。",
    "INVALID_RISK_DISTANCE": "參考價格與固定失效價之間沒有正的風險距離。",
    "NET_RR_BELOW_MINIMUM": "扣除成本與跳空緩衝後，風險報酬低於設定門檻。",
    "LEDGER_REVISION_REVIEW": "歷史價或支撐區曾修訂，需先核對原固定支撐。",
    "LEDGER_INVALIDATED": "跨日帳本記錄原固定支撐已失效。",
    "LEDGER_STALE": "本輪資料落後於跨日帳本，不能用舊結論交易。",
    "LEDGER_ERROR": "跨日追蹤未完整完成，等待修復。",
    "SEARCH_NOT_COMPLETED": "尚未完成本輪有效的尋標 agent 提交。",
    "NOT_SELECTED_BY_SEARCH": "本檔未被本輪尋標 agent 提交給決策審查。",
    "SEARCH_INTEGRITY_FAILED": "尋標提交與本輪快照不符，需重新搜尋。",
}


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _packet_for(report, item):
    packets = [p for p in report.get("agent_packets", [])
               if isinstance(p, dict) and p.get("packet_id") == item.get("packet_id")]
    if len(packets) != 1:
        return None
    packet = packets[0]
    try:
        verify_packet(packet)
        core = packet["core"]
        technical = {k: v for k, v in item.get("technical", {}).items() if k != "evidence"}
        if (core["symbol"] != item["symbol"] or core["as_of"] != report["as_of"]
                or type(core.get("context_reject")) is not bool
                or core.get("synthetic") is not report.get("synthetic")
                or core.get("observation_only") is not item.get("observation_only")
                or bool(core.get("discovery_required")) != bool(report.get("discovery_required"))
                or core.get("source_universe_hash") != report.get("source_universe_hash")
                or core["technical"] != technical
                or core["visible_series"]["price_basis"] != item.get("price_basis")
                or core["context_blockers"] != item.get("context_blockers", [])):
            return None
        return packet
    except (DataError, KeyError, TypeError, ValueError):
        return None


def _reference_plan(item, packet, report, policy):
    """Use only the packet's visible pre-anchor bars; never backfill a target."""
    technical = item.get("technical", {})
    support = technical.get("support") or {}
    entry, stop, atr = technical.get("latest_close"), support.get("lower"), support.get("atr")
    if not all(_positive(v) for v in (entry, stop, atr)) or packet is None:
        return None, ["REFERENCE_PLAN_UNAVAILABLE"]
    core = packet["core"]
    cutoff = timestamp(report["as_of"])
    bars = core["visible_series"].get("bars", [])
    visible = [b for b in bars if timestamp(b["available_at"]) <= cutoff and timestamp(b["closed_at"]) <= cutoff]
    latest = next((b for b in visible if b["session"] == technical.get("latest_session")), None)
    if latest is None or latest["close"] != entry:
        return None, ["REFERENCE_PLAN_UNAVAILABLE"]
    before_anchor = [b for b in visible if b["session"] < support.get("anchor_at", "")]
    window = before_anchor[-policy.resistance_lookback_sessions:]
    plan = {
        "currency": core["visible_series"].get("metadata", {}).get("currency"),
        "price_basis": item.get("price_basis"), "reference_session": latest["session"],
        "reference_available_at": latest["available_at"], "as_of": report["as_of"],
        "entry_reference": entry, "entry_basis": "LATEST_COMPLETED_DAILY_CLOSE_NOT_LIVE_QUOTE",
        "invalidation_reference": stop,
        "invalidation_rule": "DAILY_CLOSE_BELOW_FROZEN_SUPPORT_LOWER",
        "invalidation_note": "這是型態失效條件，不是已送出的停損單，也不保證以此價成交。",
        "target_reference": None, "target_basis": "PRE_ANCHOR_HISTORICAL_HIGH",
        "target_evidence": None, "gross_reward_risk": None, "net_reward_risk": None,
        "cost_assumptions": {"roundtrip_cost_bps": policy.roundtrip_cost_bps,
            "gap_buffer_atr": policy.gap_buffer_atr, "broker_costs_verified": False,
            "meaning": "研究用成本與跳空情境假設；不是券商費率或最大損失保證。"},
        "validity": "SNAPSHOT_ONLY_REVALIDATE_BEFORE_NEXT_SESSION",
        "revalidation_required": ["下一交易時段的實際報價與跳空", "最新公司公告與事件", "支撐仍有效與資料新鮮度"],
        "evidence_ids": [technical.get("latest_evidence_id"), *support.get("evidence_ids", [])],
    }
    if len(window) < policy.resistance_lookback_sessions:
        return plan, ["NO_HISTORICAL_RESISTANCE"]
    resistance = max(window, key=lambda b: (b["high"], b["session"]))
    target = resistance["high"]
    plan["target_reference"] = target
    plan["target_evidence"] = {"symbol": item["symbol"], "session": resistance["session"],
        "available_at": resistance["available_at"], "high": target,
        "window_start": window[0]["session"], "window_end": window[-1]["session"],
        "source": core["visible_series"].get("source"),
        "snapshot_packet_id": packet["packet_id"]}
    reasons = []
    if target <= entry:
        reasons.append("TARGET_NOT_ABOVE_REFERENCE")
    risk = entry - stop
    if risk <= 0:
        reasons.append("INVALID_RISK_DISTANCE")
        return plan, reasons
    reward = target - entry
    cost = entry * policy.roundtrip_cost_bps / 10_000
    gap = atr * policy.gap_buffer_atr
    plan.update(gross_reward_risk=reward / risk,
        net_reward_risk=(reward - cost) / (risk + gap + cost),
        roundtrip_cost_per_share=cost, gap_buffer_per_share=gap,
        nominal_risk_per_share=risk, stressed_loss_per_share=risk + gap + cost,
        net_reward_per_share=reward - cost)
    if plan["net_reward_risk"] < policy.minimum_net_rr:
        reasons.append("NET_RR_BELOW_MINIMUM")
    return plan, reasons


def attach_decisions(report: dict, bundle: dict, *, policy: DecisionPolicy | None = None) -> dict:
    """Attach deterministic conclusions in place; safe to call after ledger checks.

    Call once after pipeline.run, then again with report['ledger'] in the
    observation before_commit hook. The latter can downgrade a provisional
    conclusion when the frozen cross-day ledger finds a correction or failure.
    """
    policy = policy or DecisionPolicy()
    securities = report.get("securities", [])
    projection = [{k: v for k, v in item.items() if k != "trade_decision"} for item in securities]
    integrity = digest([o.get("packet_id", o) for o in projection]) == report.get("snapshot_id")
    integrity = integrity and (report.get("synthetic") is True) == (bundle.get("synthetic") is True)
    integrity = integrity and (report.get("observation_only") is True) == (bundle.get("observation_only") is True)
    sources = {item.get("symbol"): item for item in bundle.get("securities", []) if isinstance(item, dict)}
    requires_search = (report.get("discovery_required") is True or bundle.get("discovery_required") is True
        or any(p.get("core", {}).get("discovery_required") is True for p in report.get("agent_packets", [])))
    search_error, search_symbols = None, set()
    if requires_search:
        discovery = report.get("discovery", {})
        result = discovery.get("search")
        if discovery.get("mode") != "AGENT_SEARCH" or not isinstance(result, dict) or result.get("status") != "COMPLETED":
            search_error = "SEARCH_NOT_COMPLETED"
        else:
            try:
                from .discovery import validate_search_result
                search_symbols = {s["symbol"] for s in validate_search_result(result, report)}
            except (DataError, KeyError, TypeError, ValueError):
                search_error = "SEARCH_INTEGRITY_FAILED"
    totals = {key: 0 for key in LABELS}
    for item in securities:
        reasons = []
        technical = item.get("technical", {})
        state = technical.get("state")
        researchable = state in {"BASE_FORMING", "BOUNCE_SEEN", "RETEST_HELD", "RULE_CONFIRMED"}
        if not integrity:
            reasons.append("SNAPSHOT_INTEGRITY_FAILED")
        if requires_search:
            if search_error:
                reasons.append(search_error)
            elif item["symbol"] not in search_symbols:
                reasons.append("NOT_SELECTED_BY_SEARCH")
        if report.get("synthetic") is not False or bundle.get("synthetic") is not False:
            reasons.append("SYNTHETIC_DATA")
        if report.get("observation_only") or item.get("observation_only"):
            reasons.append("OBSERVATION_ONLY")
        expected_observation = bundle.get("observation_only") is True and sources.get(item.get("symbol"), {}).get("observation_only") is True
        if (item.get("observation_only") is True) != expected_observation:
            reasons.append("SNAPSHOT_INTEGRITY_FAILED")
        if item.get("data_status") != "VALID":
            reasons.append("OBSERVATION_ONLY" if item.get("data_status") == "OBSERVATION" else "DATA_REJECTED")
        if report.get("collection", {}).get("status") not in (None, "OK", "COMPLETE"):
            reasons.append("DATA_COLLECTION_DEGRADED")
        if state == "INVALIDATED":
            reasons.append("SUPPORT_INVALIDATED")
        elif state in {"NO_SETUP", "EXPIRED"}:
            reasons.append("NO_CURRENT_SETUP")
        elif state != "RULE_CONFIRMED":
            reasons.append("SUPPORT_NOT_CONFIRMED")
        if technical.get("position") == "EXTENDED":
            reasons.append("NOT_NEAR_SUPPORT")
        if technical.get("technical_candidate") is not True or technical.get("blockers"):
            reasons.append("TECHNICAL_GATE_BLOCKED")
        if item.get("context_blockers"):
            reasons.append("CONTEXT_GATE_BLOCKED")
        if item.get("verdict") == "REJECT":
            reasons.append("RESEARCH_REJECTED")
        elif item.get("verdict") != "RESEARCH":
            reasons.append("RESEARCH_NOT_APPROVED")
        packet = _packet_for(report, item)
        if packet is None and researchable:
            reasons.append("SNAPSHOT_INTEGRITY_FAILED")
        reviews = item.get("agent_reviews", {})
        if not isinstance(reviews, dict):
            reviews = {}
        # No packet is normal for terminal/no-setup states. Missing work is not
        # a failed model response; only supplied invalid reviews count as errors.
        accepted, errors = validate_reviews(packet, list(reviews.values())) if packet else ({}, [])
        if packet is not None:
            # Recompute the hard gate from the hashed core, rather than trust a
            # mutable report verdict. In particular, BROKEN company theses are
            # bound via context_reject and cannot be rewritten into RESEARCH.
            core = packet["core"]
            original_errors = item.get("agent_errors", [])
            if not isinstance(original_errors, list):
                original_errors = ["INVALID_ERROR_CONTRACT"]
            recomputed = decide(core["technical"], core["context_blockers"], core["context_reject"],
                                accepted, original_errors + errors)
            if item.get("verdict") != recomputed["verdict"] or item.get("reasons") != recomputed["reasons"]:
                reasons.append("SNAPSHOT_INTEGRITY_FAILED")
            if recomputed["verdict"] == "REJECT":
                reasons.append("RESEARCH_REJECTED")
        if item.get("agent_errors") or errors:
            reasons.append("AGENT_VALIDATION_FAILED")
        if researchable and (set(accepted) != ROLES or any(r["verdict"] != "RESEARCH" for r in accepted.values())):
            reasons.append("DUAL_REVIEW_REQUIRED")
        if report.get("unmatched_review_packets"):
            reasons.append("STALE_OR_UNMATCHED_REVIEWS")
        ledger = report.get("ledger", {})
        if ledger.get("errors") or ledger.get("status") == "DEGRADED":
            reasons.append("LEDGER_ERROR")
        for row in ledger.get("rows", []):
            if row.get("symbol") != item.get("symbol"):
                continue
            if row.get("change") == "REVISION_REVIEW" or row.get("revision_review"):
                reasons.append("LEDGER_REVISION_REVIEW")
            if row.get("current_state") == "INVALIDATED":
                reasons.append("LEDGER_INVALIDATED")
            if row.get("change") == "STALE_IGNORED":
                reasons.append("LEDGER_STALE")
        try:
            plan, plan_reasons = _reference_plan(item, packet, report, policy)
        except (DataError, KeyError, TypeError, ValueError):
            plan, plan_reasons = None, ["REFERENCE_PLAN_UNAVAILABLE"]
        reasons.extend(plan_reasons)
        reasons = list(dict.fromkeys(reasons))
        no_trade = {"DATA_REJECTED", "SUPPORT_INVALIDATED", "NO_CURRENT_SETUP", "NOT_NEAR_SUPPORT",
                    "RESEARCH_REJECTED", "LEDGER_INVALIDATED", "INVALID_RISK_DISTANCE", "NET_RR_BELOW_MINIMUM"}
        conclusion = "NO_TRADE" if no_trade.intersection(reasons) else "WAIT" if reasons else "CONSIDER"
        message = ("支撐、資料、雙方審查與情境風險報酬均通過，可列入人工交易評估；下一交易時段必須重驗。"
                   if conclusion == "CONSIDER" else "；".join(MESSAGES.get(r, r).rstrip("。") for r in reasons) + "。")
        item["trade_decision"] = {"conclusion": conclusion, "label": LABELS[conclusion],
            "summary": message, "reason_codes": reasons,
            "next_steps": _next_steps(reasons),
            "research_verdict": item.get("verdict"), "technical_blockers": technical.get("blockers", []),
            "context_blockers": item.get("context_blockers", []),
            "data_errors": item.get("reasons", []) if item.get("data_status") == "REJECTED" else [],
            "as_of": report["as_of"], "snapshot_id": report["snapshot_id"],
            "packet_id": item.get("packet_id"), "policy": asdict(policy),
            "reference_plan": plan, "execution_enabled": False,
            "account_loss_guard": "NOT_AVAILABLE_NO_ACCOUNT_OR_POSITION_DATA"}
        totals[conclusion] += 1
    report["decision_summary"] = {"policy": asdict(policy), "counts": totals,
        "consider_symbols": [i["symbol"] for i in securities if i["trade_decision"]["conclusion"] == "CONSIDER"],
        "execution_enabled": False, "meaning": "決策研究結論；每個交易時段重新驗證，沒有訂單或帳戶操作能力。"}
    return report


def _next_steps(reasons):
    steps = []
    if "OBSERVATION_ONLY" in reasons or "CONTEXT_GATE_BLOCKED" in reasons:
        steps.append("核對拆股／配息與歷史價格口徑、公司決算及重大公告、停牌與交易狀態。")
    if any(r in reasons for r in ("DATA_REJECTED", "DATA_COLLECTION_DEGRADED", "SNAPSHOT_INTEGRITY_FAILED",
                                  "STALE_OR_UNMATCHED_REVIEWS", "LEDGER_REVISION_REVIEW", "LEDGER_STALE", "LEDGER_ERROR")):
        steps.append("修復或核對來源與跨日記錄，以新快照重新掃描，不沿用舊評語。")
    if "DUAL_REVIEW_REQUIRED" in reasons or "AGENT_VALIDATION_FAILED" in reasons:
        steps.append("請研究員與反證員針對同一份完整快照各自重新審查。")
    if any(r in reasons for r in ("SEARCH_NOT_COMPLETED", "NOT_SELECTED_BY_SEARCH", "SEARCH_INTEGRITY_FAILED")):
        steps.append("以本輪行情快照完成尋標 agent 提交，再由獨立研究／反證 agent 審查；不可跳過尋標。")
    if "SUPPORT_NOT_CONFIRMED" in reasons:
        steps.append("等待另日回測守住固定支撐，且後續完整日線收盤突破回測日高點。")
    if any(r in reasons for r in ("NO_HISTORICAL_RESISTANCE", "TARGET_NOT_ABOVE_REFERENCE", "NET_RR_BELOW_MINIMUM", "NOT_NEAR_SUPPORT")):
        steps.append("等待價格與已知阻力提供足夠空間，或取得更完整的可見歷史；不為通過門檻上調目標。")
    if "SUPPORT_INVALIDATED" in reasons or "LEDGER_INVALIDATED" in reasons:
        steps.append("放棄本支撐情境；不得下移原支撐讓同一情境復活。")
    if not steps:
        steps.append("下一交易時段以新報價、公告及完整快照重新驗證。")
    return steps


def render_decisions_markdown(report: dict) -> str:
    """Render the added conclusions without changing the research verdict table."""
    if "decision_summary" not in report:
        return ""
    summary = report["decision_summary"]
    counts = summary["counts"]
    lines = ["## 是否交易的研究結論", "",
        f"可考慮 {counts['CONSIDER']} 檔；等待 {counts['WAIT']} 檔；目前不交易 {counts['NO_TRADE']} 檔。",
        "", "這是截止當時的決策研究。入場參考使用完整日線收盤；下一交易時段必須重新驗證，沒有下單能力。",
        "", "| 標的 | 結論 | 參考入場 | 固定失效價 | 歷史阻力目標 | 情境淨 RR |",
        "|---|---|---:|---:|---:|---:|"]
    def fmt(value):
        return f"{value:,.3f}" if type(value) in (int, float) else "—"
    def safe(value):
        return str(value).replace("|", "/").replace("\n", " ")
    for item in report["securities"]:
        decision = item.get("trade_decision", {})
        plan = decision.get("reference_plan") or {}
        lines.append(f"| {safe(item['symbol'])} | {safe(decision.get('label', '—'))} | "
            f"{fmt(plan.get('entry_reference'))} | {fmt(plan.get('invalidation_reference'))} | "
            f"{fmt(plan.get('target_reference'))} | {fmt(plan.get('net_reward_risk'))} |")
    for item in report["securities"]:
        decision = item.get("trade_decision", {})
        plan = decision.get("reference_plan") or {}
        lines.extend(["", f"### {safe(item['symbol'])} · {safe(decision.get('label', '—'))}", "",
            safe(decision.get("summary", "")), "",
            "硬性阻擋：" + ("、".join(decision.get("reason_codes", [])) or "無"), ""])
        if plan:
            assumption = plan["cost_assumptions"]
            lines.extend([f"價位口徑：{safe(plan.get('currency', '—'))} / {safe(plan.get('price_basis', '—'))}；"
                          f"完整日線：{safe(plan['reference_session'])}。",
                f"情境成本：來回 {assumption['roundtrip_cost_bps']:g} bps，跳空緩衝 {assumption['gap_buffer_atr']:g} ATR；"
                "券商實際費率尚未核實。固定失效價是收盤型態條件，不保證成交價。", ""])
        lines.append("後續條件：" + "；".join(decision.get("next_steps", [])))
    return "\n".join(lines) + "\n"
