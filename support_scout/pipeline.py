from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import timedelta
from copy import deepcopy

from .agents import decide, make_packet, validate_reviews
from .contracts import DataError, Rules, TAIPEI, calendar_at, digest, load_series, timestamp, visible_series
from .support import detect_support


def context_at(bundle: dict, item: dict, as_of, rules: Rules) -> tuple[list, list[str], bool]:
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
                if timestamp(value.get("events_reviewed_through")) < as_of + timedelta(days=rules.event_buffer_days):
                    raise DataError("EVENT_COVERAGE_INSUFFICIENT")
                events = value.get("events")
                if not isinstance(events, list):
                    raise DataError("EVENTS_UNKNOWN")
                for event in events:
                    if not event.get("source") or timestamp(event.get("announced_at")) > as_of:
                        raise DataError("EVENT_NOT_KNOWN_AT_CUTOFF")
                    when = timestamp(event.get("event_at"))
                    if as_of - timedelta(days=1) <= when <= as_of + timedelta(days=rules.event_buffer_days):
                        blockers.append("NEAR_MATERIAL_EVENT")
            except (DataError, AttributeError, TypeError):
                blockers.append("EVENT_CALENDAR_UNKNOWN")
                continue
        evidence.append({"id": f"{item['symbol']}:{kind}", "kind": kind, "available_at": value["available_at"], "values": value})
    return evidence, sorted(set(blockers)), reject


def run(bundle: dict, *, as_of: str | None = None, rules: Rules | None = None, reviews: list | None = None) -> dict:
    if not isinstance(bundle, dict):
        raise DataError("BUNDLE_MUST_BE_OBJECT")
    if bundle.get("schema_version") != 1:
        raise DataError("UNSUPPORTED_SCHEMA_VERSION")
    if not isinstance(bundle.get("collection", {}), dict):
        raise DataError("INVALID_COLLECTION_METADATA")
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
            observation = bundle.get("observation_only") is True and item.get("observation_only") is True
            series = load_series(item, cutoff, calendar, allow_observation=observation)
            technical = detect_support(series, rules)
            context_evidence, context_blockers, context_reject = context_at(bundle, item, cutoff, rules)
            if series.metadata.get("point_in_time") is not True:
                context_blockers.append("POINT_IN_TIME_NOT_VERIFIED")
            if observation:
                context_blockers.extend(["OBSERVATION_ONLY", "CORPORATE_ACTIONS_UNVERIFIED", "TRADABILITY_UNVERIFIED"])
            if bundle.get("collection", {}).get("status") not in (None, "OK", "COMPLETE"):
                context_blockers.append("DATA_COLLECTION_DEGRADED")
            evidence = technical["evidence"] + context_evidence
            required = [e["id"] for e in evidence if e["kind"] in {"ANCHOR", "BOUNCE", "RETEST", "CONFIRMATION", "LATEST", "DOSSIER", "MARKET"}]
            core = {"schema_version": 1, "as_of": cutoff.isoformat(), "symbol": symbol,
                    "synthetic": bundle.get("synthetic") is True, "observation_only": observation,
                    "calendar": {k: v for k, v in bundle["calendar"].items() if k != "sessions"},
                    "calendar_visible_sessions": calendar,
                    "universe_version": bundle.get("universe_version", "unversioned"),
                    "rules": asdict(rules), "visible_series": visible_series(series),
                    "technical": {k: v for k, v in technical.items() if k != "evidence"},
                    "context_blockers": context_blockers, "evidence": evidence,
                    "required_confirmation_evidence": required}
            packet = make_packet(core)
            own_reviews = [r for r in review_rows if isinstance(r, dict) and r.get("packet_id") == packet["packet_id"]]
            accepted, errors = validate_reviews(packet, own_reviews)
            decision = decide(technical, context_blockers, context_reject, accepted, errors)
            outputs.append({"symbol": symbol, "name": series.name, "exchange": series.exchange,
                            "packet_id": packet["packet_id"], "data_status": "OBSERVATION" if observation else "VALID",
                            "price_basis": series.price_basis, "observation_only": observation,
                            "technical": technical, "context_blockers": context_blockers,
                            "agent_reviews": accepted, "agent_errors": errors, **decision})
            if technical["state"] not in {"INSUFFICIENT_DATA", "NO_SETUP", "INVALIDATED", "EXPIRED"}:
                packets.append(packet)
        except (DataError, KeyError, TypeError, ValueError) as exc:
            outputs.append({"symbol": symbol, "data_status": "REJECTED", "verdict": "REJECT", "reasons": [str(exc)]})
    valid_packet_ids = {o["packet_id"] for o in outputs if "packet_id" in o}
    unmatched = [r.get("packet_id") if isinstance(r, dict) else "MALFORMED" for r in review_rows
                 if not isinstance(r, dict) or r.get("packet_id") not in valid_packet_ids]
    queue = sorted([o for o in outputs if o.get("technical", {}).get("technical_candidate")],
                   key=lambda o: (-o["technical"]["priority_score"], o["symbol"]))
    shortlist = [o["symbol"] for o in queue if o["verdict"] == "RESEARCH"]
    return {"schema_version": 1, "purpose": "TAIWAN_SUPPORT_RESEARCH_ONLY", "as_of": cutoff.isoformat(),
            "as_of_taipei": cutoff.astimezone(TAIPEI).isoformat(), "rules": asdict(rules),
            "snapshot_id": digest([o.get("packet_id", o) for o in outputs]),
            "universe_version": bundle.get("universe_version", "unversioned"),
            "synthetic": bundle.get("synthetic") is True,
            "observation_only": bundle.get("observation_only") is True,
            "collection": deepcopy(bundle.get("collection", {})),
            "securities": outputs, "technical_queue": [o["symbol"] for o in queue],
            "reviewed_shortlist": shortlist, "agent_packets": packets, "unmatched_review_packets": unmatched,
            "limitations": ["RULES_AND_RANKING_NOT_BACKTEST_VALIDATED", "NO_ESTIMATED_WIN_PROBABILITY",
                            "RAW_OBSERVATION_REQUIRES_CORPORATE_ACTION_AND_EVENT_REVIEW",
                            "MANUAL_REVIEW_IDENTITIES_ARE_SELF_REPORTED"]}


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
        lines.append(f"| {item['symbol']} | {item['data_status']} | {t.get('state', '—')} | {t.get('position', '—')} | {t.get('priority_score', '—')} | {item['verdict']} | {reason} |")
    lines.extend(["", "技術研究佇列：" + (", ".join(report["technical_queue"]) or "無"),
                  "", "兩方審查完成的名單：" + (", ".join(report["reviewed_shortlist"]) or "無"),
                  "", "observe 取得官方日線並執行雙方研究；原始價未核實時維持 OBSERVATION ONLY。", ""])
    if "desk" in report:
        d = report["desk"]
        lines.extend([f"Desk：{d['status']}；模式：{d['mode']}；呼叫：{d['call_count']}；run：{d['run_id']}", ""])
    return "\n".join(lines)
