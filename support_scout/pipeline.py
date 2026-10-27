from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import timedelta

from .agents import decide, make_packet, validate_reviews
from .contracts import DataError, Rules, TAIPEI, calendar_at, digest, load_series, timestamp, visible_series
from .support import detect_support


def context_at(bundle: dict, item: dict, as_of, rules: Rules, *, latest_closed_at) -> tuple[list, list[str], bool]:
    blockers, evidence = [], []
    reject = False
    market = bundle.get("market_context", {})
    dossier = item.get("dossier", {})
    for kind, value in (("MARKET", market), ("DOSSIER", dossier)):
        try:
            valid = (isinstance(value, dict) and bool(value.get("source")) and
                     timedelta(0) <= as_of - timestamp(value.get("available_at")) <= timedelta(days=7))
        except DataError:
            valid = False
        if not valid:
            blockers.append(kind + "_UNKNOWN_OR_STALE")
            continue
        # The entire dossier must be known at the cutoff, including event announcements.
        if kind == "MARKET":
            if value.get("stress") not in {"NORMAL", "ELEVATED"}:
                blockers.append("MARKET_STRESS_UNKNOWN")
        else:
            if value.get("thesis_status") not in {"INTACT", "BROKEN"}:
                blockers.append("THESIS_UNKNOWN")
            reject = value.get("thesis_status") == "BROKEN"
            try:
                if timestamp(value.get("events_reviewed_from")) > latest_closed_at:
                    raise DataError("EVENT_LOOKBACK_INSUFFICIENT")
                if timestamp(value.get("events_reviewed_through")) < as_of + timedelta(days=rules.event_buffer_days):
                    raise DataError("EVENT_COVERAGE_INSUFFICIENT")
                events = value.get("events")
                if not isinstance(events, list):
                    raise DataError("EVENTS_UNKNOWN")
                for event in events:
                    announced = timestamp(event.get("announced_at"))
                    if not event.get("source") or announced > as_of:
                        raise DataError("EVENT_NOT_KNOWN_AT_CUTOFF")
                    when = max(timestamp(event.get("event_at")), announced)
                    if latest_closed_at <= when <= as_of:
                        blockers.append("MATERIAL_EVENT_NOT_REFLECTED_IN_LATEST_BAR")
                    elif as_of < when <= as_of + timedelta(days=rules.event_buffer_days):
                        blockers.append("NEAR_MATERIAL_EVENT")
            except (DataError, AttributeError, TypeError):
                blockers.append("EVENT_CALENDAR_UNKNOWN")
                continue
        evidence.append({"id": f"{item['symbol']}:{kind}", "kind": kind, "available_at": value["available_at"], "values": value})
    return evidence, sorted(set(blockers)), reject


def run(bundle: dict, *, as_of: str | None = None, rules: Rules | None = None, reviews: list | None = None) -> dict:
    if not isinstance(bundle, dict):
        raise DataError("SNAPSHOT_MUST_BE_OBJECT")
    if bundle.get("schema_version") != 1:
        raise DataError("UNSUPPORTED_SCHEMA_VERSION")
    cutoff = timestamp(as_of or bundle.get("as_of"))
    rules = rules or Rules()
    calendar = calendar_at(bundle, cutoff)
    raw_items = bundle.get("securities")
    if not isinstance(raw_items, list) or not raw_items:
        raise DataError("UNIVERSE_REQUIRED")
    review_rows = reviews if reviews is not None else []
    if not isinstance(review_rows, list):
        raise DataError("REVIEWS_MUST_BE_LIST")
    symbols = [s.get("symbol") for s in raw_items if isinstance(s, dict)]
    counts = Counter(s for s in symbols if isinstance(s, str))
    outputs, packets = [], []
    for item in raw_items:
        symbol = item.get("symbol", "UNKNOWN") if isinstance(item, dict) else "UNKNOWN"
        try:
            if not isinstance(symbol, str) or counts.get(symbol, 0) != 1:
                raise DataError("DUPLICATE_OR_INVALID_SYMBOL")
            series = load_series(item, cutoff, calendar)
            technical = detect_support(series, rules)
            context_evidence, context_blockers, context_reject = context_at(
                bundle, item, cutoff, rules, latest_closed_at=timestamp(series.bars[-1].closed_at))
            if series.metadata.get("point_in_time") is not True:
                context_blockers.append("POINT_IN_TIME_NOT_VERIFIED")
            evidence = technical["evidence"] + context_evidence
            required = [e["id"] for e in evidence if e["kind"] in {"ANCHOR", "BOUNCE", "RETEST", "CONFIRMATION", "LATEST", "DOSSIER", "MARKET"}]
            core = {"schema_version": 1, "as_of": cutoff.isoformat(), "symbol": symbol,
                    "universe_version": bundle.get("universe_version", "unversioned"),
                    "calendar": {"source": bundle["calendar"]["source"],
                                 "available_at": bundle["calendar"]["available_at"],
                                 "coverage_through": cutoff.astimezone(TAIPEI).date().isoformat(),
                                 "sessions": calendar},
                    "rules": asdict(rules), "visible_series": visible_series(series),
                    "technical": {k: v for k, v in technical.items() if k != "evidence"},
                    "context_blockers": context_blockers, "evidence": evidence,
                    "required_confirmation_evidence": required}
            packet = make_packet(core)
            own_reviews = [r for r in review_rows if isinstance(r, dict) and r.get("packet_id") == packet["packet_id"]]
            accepted, errors = validate_reviews(packet, own_reviews)
            decision = decide(technical, context_blockers, context_reject, accepted, errors)
            outputs.append({"symbol": symbol, "name": series.name, "exchange": series.exchange,
                            "packet_id": packet["packet_id"], "data_status": "VALID",
                            "technical": technical, "context_blockers": context_blockers,
                            "agent_reviews": accepted, "agent_errors": errors, **decision})
            if technical["state"] not in {"INSUFFICIENT_DATA", "NO_SETUP", "INVALIDATED", "EXPIRED"}:
                packets.append(packet)
        except (DataError, KeyError, TypeError, ValueError) as exc:
            outputs.append({"symbol": symbol, "data_status": "REJECTED", "verdict": "REJECT", "reasons": [str(exc)]})
    valid_packet_ids = {o["packet_id"] for o in outputs if "packet_id" in o}
    unmatched = []
    for review in review_rows:
        packet_id = review.get("packet_id") if isinstance(review, dict) else None
        if not isinstance(packet_id, str):
            unmatched.append("MALFORMED")
        elif packet_id not in valid_packet_ids:
            unmatched.append(packet_id)
    queue = sorted([o for o in outputs if o.get("technical", {}).get("technical_candidate")],
                   key=lambda o: (-o["technical"]["priority_score"], o["symbol"]))
    shortlist = [o["symbol"] for o in queue if o["verdict"] == "RESEARCH"]
    return {"schema_version": 1, "purpose": "TAIWAN_SUPPORT_RESEARCH_ONLY", "as_of": cutoff.isoformat(),
            "as_of_taipei": cutoff.astimezone(TAIPEI).isoformat(), "rules": asdict(rules),
            "snapshot_id": digest([o.get("packet_id", o) for o in outputs]),
            "universe_version": bundle.get("universe_version", "unversioned"),
            "synthetic": bundle.get("synthetic") is True,
            "securities": outputs, "technical_queue": [o["symbol"] for o in queue],
            "reviewed_shortlist": shortlist, "agent_packets": packets, "unmatched_review_packets": unmatched,
            "limitations": ["RULES_AND_RANKING_NOT_BACKTEST_VALIDATED", "NO_ESTIMATED_WIN_PROBABILITY",
                            "PHASE_ONE_EXPORTS_AGENT_TASKS_AND_IMPORTS_REVIEWS_NO_AUTONOMOUS_LLM_RUNNER"]}


def render_markdown(report: dict) -> str:
    label = "合成資料示範（不是真實標的行情）" if report["synthetic"] else "台股支撐研究"
    lines = [f"# {label}", "", f"資料截止：{report['as_of_taipei']}", "",
             f"規則：{report['rules']['version']}；快照：{report['snapshot_id'][:16]}", "",
             "RESEARCH 代表值得進一步研究；規則成立與分數均不代表獲利機率。", "",
             "| 代號 | 資料 | 支撐狀態 | 價格位置 | 研究排序分數 | 結論 | 原因 |",
             "|---|---|---|---|---:|---|---|"]
    for item in report["securities"]:
        t = item.get("technical", {})
        reason = ", ".join(item["reasons"]).replace("|", "/").replace("\n", " ")
        score = t.get("priority_score")
        lines.append(f"| {item['symbol']} | {item['data_status']} | {t.get('state', '—')} | {t.get('position', '—')} | {score if score is not None else '—'} | {item['verdict']} | {reason} |")
    lines.extend(["", "技術研究佇列：" + (", ".join(report["technical_queue"]) or "無"),
                  "", "兩方審查完成的名單：" + (", ".join(report["reviewed_shortlist"]) or "無"),
                  "", "第一階段輸出 agent 任務包與匯入審查結果；尚未接自動模型執行器。", ""])
    return "\n".join(lines)
