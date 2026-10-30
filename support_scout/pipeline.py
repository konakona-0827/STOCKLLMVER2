from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import timedelta
from copy import deepcopy

from .agents import decide, make_packet, validate_reviews
from .contracts import DataError, Rules, TAIPEI, TOKYO, calendar_at, digest, load_series, timestamp, visible_series
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
    market = bundle.get("market", "TW")
    rules = rules or Rules()
    registry = None
    public_policy = None
    opportunity_policy = None
    if "opportunity_policy" in bundle:
        from .opportunity import load_policy as load_opportunity
        opportunity_policy = load_opportunity(bundle["opportunity_policy"])
    communication_policy = None
    if "communication_policy" in bundle:
        from .communication import load_policy as load_communication
        communication_policy = load_communication(bundle["communication_policy"])
    portfolio = deepcopy(bundle.get("portfolio_snapshot"))
    sizing_policy = None
    if "sizing_policy" in bundle:
        from .sizing import load_settings as load_sizing
        sizing_policy = load_sizing(bundle["sizing_policy"])
    if portfolio is not None:
        from .portfolio import verify_snapshot
        portfolio = verify_snapshot(portfolio, as_of=cutoff.isoformat())
    if market == "JP":
        from .patterns import load_registry
        registry = load_registry(bundle.get("pattern_registry"))
        if "public_information_policy" in bundle:
            from .public_information import load_policy
            public_policy = load_policy(bundle["public_information_policy"])
            if public_policy["enabled"] and bundle.get("synthetic") is not False:
                raise DataError("PUBLIC_WEB_FORBIDDEN_IN_SYNTHETIC_CASE")
    calendar = calendar_at(bundle, cutoff)
    raw_items = bundle.get("securities")
    if not isinstance(raw_items, list) or not raw_items:
        raise DataError("UNIVERSE_REQUIRED")
    review_rows = reviews if reviews is not None else []
    if not isinstance(review_rows, list):
        raise DataError("REVIEWS_MUST_BE_LIST")
    symbols = [s.get("symbol") for s in raw_items if isinstance(s, dict)]
    counts = Counter(s for s in symbols if isinstance(s, str))
    outputs, packets, all_packets = [], [], []
    for item in raw_items:
        symbol = item.get("symbol", "UNKNOWN") if isinstance(item, dict) else "UNKNOWN"
        try:
            if not isinstance(symbol, str) or counts.get(symbol, 0) != 1:
                raise DataError("DUPLICATE_OR_INVALID_SYMBOL")
            observation = bundle.get("observation_only") is True and item.get("observation_only") is True
            series = load_series(item, cutoff, calendar, allow_observation=observation, market=market)
            technical = detect_support(series, rules)
            patterns = []
            if registry is not None:
                from .patterns import evaluate_patterns
                patterns = evaluate_patterns(series, cutoff, registry, item.get("pattern_context"))
            context_evidence, context_blockers, context_reject = context_at(bundle, item, cutoff, rules)
            if series.metadata.get("point_in_time") is not True:
                context_blockers.append("POINT_IN_TIME_NOT_VERIFIED")
            if observation:
                context_blockers.extend(["OBSERVATION_ONLY", "CORPORATE_ACTIONS_UNVERIFIED", "TRADABILITY_UNVERIFIED"])
            source_blockers = series.metadata.get("observation_blockers") or []
            if not isinstance(source_blockers, list) or any(not isinstance(b, str) for b in source_blockers):
                raise DataError("INVALID_OBSERVATION_BLOCKERS")
            context_blockers.extend(source_blockers)
            context_blockers = sorted(set(context_blockers))
            if bundle.get("collection", {}).get("status") not in (None, "OK", "COMPLETE"):
                context_blockers.append("DATA_COLLECTION_DEGRADED")
            evidence = technical["evidence"] + context_evidence
            evidence.extend(e for pattern in patterns for e in pattern["evidence"])
            required = [e["id"] for e in evidence if e["kind"] in {"ANCHOR", "BOUNCE", "RETEST", "CONFIRMATION", "LATEST", "DOSSIER", "MARKET"}]
            required.extend(e for pattern in patterns if pattern["research_candidate"] for e in pattern["required_evidence_ids"])
            core = {"schema_version": 1, "as_of": cutoff.isoformat(), "symbol": symbol,
                    "market": market,
                    "discovery_required": bundle.get("discovery_required") is True,
                    "source_universe_hash": digest(bundle["source_universe"]) if bundle.get("discovery_required") else None,
                    "synthetic": bundle.get("synthetic") is True, "observation_only": observation,
                    "calendar": {k: v for k, v in bundle["calendar"].items() if k != "sessions"},
                    "calendar_visible_sessions": calendar,
                    "universe_version": bundle.get("universe_version", "unversioned"),
                    "rules": asdict(rules), "visible_series": visible_series(series),
                    "technical": {k: v for k, v in technical.items() if k != "evidence"},
                    "context_blockers": context_blockers, "context_reject": context_reject, "evidence": evidence,
                    "required_confirmation_evidence": required}
            if registry is not None:
                core["patterns"] = patterns
                core["pattern_registry_digest"] = digest(registry)
                core["support_model_boundaries"] = {"technical": "LEGACY_STRICT_ATR_RETEST_TRADING_GATE",
                    "patterns": "INDEPENDENT_OPPORTUNITY_HYPOTHESES_DO_NOT_OVERRIDE_TRADING_GATE"}
                if "analysis_context" in bundle:
                    core["analysis_context"] = deepcopy(bundle["analysis_context"])
            if public_policy is not None:
                core["public_information_policy"] = deepcopy(public_policy)
            if communication_policy is not None:
                core["communication_policy"] = deepcopy(communication_policy)
            if opportunity_policy is not None:
                core["opportunity_policy"] = deepcopy(opportunity_policy)
                from .opportunity import _reference_plan, STAGES
                active_pattern = next((p for p in patterns if p["pattern_id"] in opportunity_policy["pattern_ids"]
                    and p.get("stage") in STAGES and p.get("research_candidate")), None)
                core["opportunity_reference_plan"] = _reference_plan(core, active_pattern, opportunity_policy) if active_pattern else None
            position = (portfolio or {}).get("positions", {}).get(symbol)
            if portfolio is not None:
                core["portfolio_snapshot_digest"] = portfolio["digest"]
                if position:
                    core["portfolio_position"] = deepcopy(position)
            packet = make_packet(core)
            all_packets.append(packet)
            own_reviews = [r for r in review_rows if isinstance(r, dict) and r.get("packet_id") == packet["packet_id"]]
            accepted, errors = validate_reviews(packet, own_reviews)
            decision = decide(technical, context_blockers, context_reject, accepted, errors)
            outputs.append({"symbol": symbol, "name": series.name, "exchange": series.exchange,
                            "currency": series.metadata["currency"],
                            "packet_id": packet["packet_id"], "data_status": "OBSERVATION" if observation else "VALID",
                            "price_basis": series.price_basis, "observation_only": observation,
                            "technical": technical, "context_blockers": context_blockers,
                            "agent_reviews": accepted, "agent_errors": errors, **decision})
            if registry is not None:
                outputs[-1]["patterns"] = patterns
            pattern_candidate = any(p["research_candidate"] for p in patterns)
            from .opportunity import active_candidate
            if (technical["state"] not in {"INSUFFICIENT_DATA", "NO_SETUP", "INVALIDATED", "EXPIRED"}
                    or (pattern_candidate and technical["state"] not in {"INVALIDATED", "INSUFFICIENT_DATA"})
                    or active_candidate(core)
                    or (position and technical["state"] != "INSUFFICIENT_DATA")):
                packets.append(packet)
        except (DataError, KeyError, TypeError, ValueError) as exc:
            source_errors = item.get("collection_errors", []) if isinstance(item, dict) else []
            source_errors = [e for e in source_errors if isinstance(e, str)] if isinstance(source_errors, list) else []
            outputs.append({"symbol": symbol, "data_status": "REJECTED", "verdict": "REJECT",
                            "reasons": list(dict.fromkeys([str(exc), *source_errors]))})
    valid_packet_ids = {o["packet_id"] for o in outputs if "packet_id" in o}
    unmatched = [r.get("packet_id") if isinstance(r, dict) else "MALFORMED" for r in review_rows
                 if not isinstance(r, dict) or r.get("packet_id") not in valid_packet_ids]
    queue = sorted([o for o in outputs if o.get("technical", {}).get("technical_candidate")],
                   key=lambda o: (-o["technical"]["priority_score"], o["symbol"]))
    shortlist = [o["symbol"] for o in queue if o["verdict"] == "RESEARCH"]
    report = {"schema_version": 1, "purpose": "JAPAN_SUPPORT_DECISION_ONLY" if market == "JP" else "TAIWAN_SUPPORT_RESEARCH_ONLY",
            "market": market, "as_of": cutoff.isoformat(), "as_of_tokyo": cutoff.astimezone(TOKYO).isoformat(),
            "as_of_taipei": cutoff.astimezone(TAIPEI).isoformat(), "rules": asdict(rules),
            "snapshot_id": digest([o.get("packet_id", o) for o in outputs]),
            "universe_version": bundle.get("universe_version", "unversioned"),
            "synthetic": bundle.get("synthetic") is True,
            "observation_only": bundle.get("observation_only") is True,
            "collection": deepcopy(bundle.get("collection", {})),
            "discovery_required": bundle.get("discovery_required") is True,
            "source_universe_hash": digest(bundle["source_universe"]) if bundle.get("discovery_required") else None,
            "securities": outputs, "technical_queue": [o["symbol"] for o in queue],
            "reviewed_shortlist": shortlist, "agent_packets": packets, "all_packets": all_packets, "unmatched_review_packets": unmatched,
            "limitations": ["RULES_AND_RANKING_NOT_BACKTEST_VALIDATED", "NO_ESTIMATED_WIN_PROBABILITY",
                            "RAW_OBSERVATION_REQUIRES_CORPORATE_ACTION_AND_EVENT_REVIEW",
                            "MANUAL_REVIEW_IDENTITIES_ARE_SELF_REPORTED"]}
    if "discovery" in bundle:
        report["discovery"] = deepcopy(bundle["discovery"])
    if registry is not None:
        report["pattern_registry"] = registry
        report["pattern_registry_digest"] = digest(registry)
        from .opportunity import active_candidate
        active_symbols = {p["core"]["symbol"] for p in packets if active_candidate(p["core"])}
        report["pattern_opportunity_queue"] = [o["symbol"] for o in outputs
            if (o.get("verdict") != "REJECT" or o["symbol"] in active_symbols)
            and any(p["research_candidate"] for p in o.get("patterns", []))]
    if public_policy is not None:
        report["public_information_policy"] = deepcopy(public_policy)
    if communication_policy is not None:
        report["communication_policy"] = communication_policy
    if opportunity_policy is not None:
        report["opportunity_policy"] = opportunity_policy
    if portfolio is not None:
        report["portfolio_snapshot"] = portfolio
    if sizing_policy is not None:
        report["sizing_policy"] = sizing_policy
    if market == "JP":
        from .decision import attach_decisions
        attach_decisions(report, bundle)
        if opportunity_policy is not None:
            from .opportunity import attach_opportunities
            attach_opportunities(report, bundle)
        if portfolio is not None:
            from .portfolio import attach_manual_actions
            attach_manual_actions(report)
        if sizing_policy is not None:
            from .sizing import attach_execution_plans
            attach_execution_plans(report, sizing_policy)
    return report


def render_markdown(report: dict, *, include_decisions: bool = True) -> str:
    japan = report.get("market") == "JP"
    label = "合成資料示範（不是真實標的行情）" if report["synthetic"] else "日股支撐決策" if japan else "台股支撐研究"
    lines = [f"# {label}", "", f"資料截止：{report['as_of_tokyo'] if japan else report['as_of_taipei']}", "",
             f"規則：{report['rules']['version']}；快照：{report['snapshot_id'][:16]}", "",
             "RESEARCH 代表值得進一步研究；規則成立與分數均不代表獲利機率。", "",
             "| 代號 | 資料 | 支撐狀態 | 價格位置 | 研究排序分數 | 結論 | 原因 |",
             "|---|---|---|---|---:|---|---|"]
    for item in report["securities"]:
        t = item.get("technical", {})
        reason = ", ".join(item["reasons"]).replace("|", "/").replace("\n", " ")
        conclusion = item.get("trade_decision", {}).get("conclusion", item["verdict"])
        lines.append(f"| {item['symbol']} | {item['data_status']} | {t.get('state', '—')} | {t.get('position', '—')} | {t.get('priority_score', '—')} | {conclusion} | {reason} |")
    lines.extend(["", "技術研究佇列：" + (", ".join(report["technical_queue"]) or "無"),
                  "", "兩方審查完成的名單：" + (", ".join(report["reviewed_shortlist"]) or "無"),
                  "", "Yahoo Finance 為暫時觀察來源；缺口未解除前不形成可交易結論。" if japan else "observe 取得官方日線並執行雙方研究；原始價未核實時維持 OBSERVATION ONLY。", ""])
    if "discovery" in report:
        discovery = report["discovery"]
        coverage = discovery.get("coverage", {})
        search = discovery.get("search") or {}
        lines.extend([f"尋標：{discovery['mode']} / {search.get('status', 'NOT_RUN')}；"
            f"本輪 {coverage.get('scanned_count', '?')} / 母池 {coverage.get('universe_count', '?')} 檔。",
            "SEARCH 提交：" + (", ".join(s["symbol"] for s in search.get("selection", [])) or "無"),
            f"總模型呼叫：{discovery.get('total_call_count', 0)} / {discovery.get('max_total_calls', 0)}", ""])
    if japan and include_decisions:
        from .decision import render_decisions_markdown
        lines.append(render_decisions_markdown(report))
    if "desk" in report:
        d = report["desk"]
        lines.extend([f"Desk：{d['status']}；模式：{d['mode']}；呼叫：{d['call_count']}；run：{d['run_id']}", ""])
        if report.get("public_information_policy", {}).get("enabled"):
            lines.extend([f"已記錄的公開搜尋工具完成：{d.get('web_tool_call_count', 0)} 次；"
                f"用量紀錄{'完整' if d.get('web_usage_complete', False) else '有缺口，失敗請求用量未知'}；"
                "來源與發布時間保存於各角色的public_information。", ""])
            for item in report["securities"]:
                for role, review in item.get("agent_reviews", {}).items():
                    for f in review.get("public_information", {}).get("findings", []):
                        lines.append(f"- {item['symbol']} / {role} / {f['category']}：{f['fact']}（{f['published_at']}） {f['url']}")
            lines.append("")
    if report.get("pattern_registry"):
        lines.extend(["## Pattern 機會層", "", "階段是研究線索，不等於交易資格或勝率。", "",
            "| 標的 | Pattern／版本 | 機會階段 | 無新低交易日 | 支撐測試 | 量比 |", "|---|---|---|---:|---:|---:|"])
        for item in report["securities"]:
            for pattern in item.get("patterns", []):
                m = pattern["metrics"]
                ratio = m.get("volume_contraction_ratio")
                lines.append(f"| {item['symbol']} | {pattern['pattern_id']} / {pattern['version']} | {pattern['stage_label']} | {m.get('no_new_low_sessions','—')} | {m.get('support_test_count','—')} | {round(ratio,3) if ratio is not None else '—'} |")
        lines.append("")
    if report.get("decision_brief"):
        brief = report["decision_brief"]
        lines.extend(["## Python 決策總表", "", f"設定本金：{brief['capital_jpy']} JPY；單筆情境虧損上限：{brief['per_trade_risk_budget_jpy']} JPY。", "",
            "| 標的 | 結論 | 建議投入JPY | 估算股數 | 進場／失效／主要阻力 | 情境虧損JPY |", "|---|---|---:|---:|---|---:|"])
        for p in brief["rows"]:
            lines.append(f"|{p['symbol']}|{p['label']}|{p['suggested_amount_jpy']}|{p['suggested_shares']}|{p['entry_reference']} / {p['invalidation_reference']} / {p['target_reference']}|{p['estimated_loss_jpy']}|")
        lines.append("")
    return "\n".join(lines)
