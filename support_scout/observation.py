"""Collect end-of-day snapshots, run bounded research and publish decision evidence."""
from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import hashlib
import json

from .contracts import DataError, Rules, digest, strict_json_loads
from .desk import DeskConfig, run_desk
from .ledger import record_observation
from .pipeline import render_markdown, run
from .providers import OpenAIProvider
from .reporting import render_html
from .settings import ROOT, DEFAULT_SETTINGS, load_settings, openai_config
from .storage import atomic_json, publish_run, publish_latest, pointer_lock
from .twse import build_snapshot as build_tw_snapshot

JST = timezone(timedelta(hours=9))


def _cursor_read(path, universe_id, symbols=None):
    if not path.exists():
        return 0
    state = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or type(state.get("cursor")) is not int or state["cursor"] < 0:
        raise DataError("INVALID_DISCOVERY_CURSOR")
    if state.get("universe_id") == universe_id:
        return state["cursor"]
    if symbols:
        last = state.get("last_symbol")
        return (symbols.index(last) + 1) % len(symbols) if last in symbols else state["cursor"] % len(symbols)
    return 0


def _cursor_commit(path, universe_id, previous, following, *, symbols=None, universe_as_of=None, expected_revision=None):
    with pointer_lock(path.with_suffix(".lock")):
        state = strict_json_loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        revision = state.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise DataError("INVALID_DISCOVERY_CURSOR_REVISION")
        if expected_revision is not None and revision != expected_revision:
            return
        if universe_as_of and (state.get("universe_as_of") or "") > universe_as_of:
            return
        if _cursor_read(path, universe_id, symbols) == previous:
            atomic_json(path, {"universe_id": universe_id, "cursor": following,
                "revision": revision + 1,
                "last_symbol": symbols[(following - 1) % len(symbols)] if symbols else None,
                "universe_as_of": universe_as_of,
                "updated_at": datetime.now(JST).isoformat()})


def _agent_status(report):
    desk = report["desk"]
    return ("FAILED" if desk["error_count"] else "NO_ELIGIBLE_PACKETS" if not desk["call_count"]
        else "PARTIAL_BUDGET" if desk["deferred_symbols"] else "COMPLETED")


def observe(*, symbols=None, months=8, cache_dir=None, output_root=None, db_path=None,
            settings_path=DEFAULT_SETTINGS, agents=None, model=None, max_packets=5, max_calls=10,
            timeout=15, bundle=None, builder=None, researcher=None, reviewer=None, rules=None, progress=None,
            market="JP", discover=None, scan_limit=20, search_provider=None, universe=None,
            universe_loader=None, cursor_path=None, public_information=None,
            universe_path=None, universe_source="fixed", portfolio_path=None):
    settings = load_settings(Path(settings_path))
    agents = settings.get("run_agents", False) if agents is None else agents
    rules = rules or Rules()
    config = DeskConfig(max_packets, max_calls)
    output_root = Path(output_root or ROOT / "output" / "observations")
    db_path = Path(db_path or ROOT / "local_data" / "observations.sqlite3")
    market = bundle.get("market", "TW") if bundle is not None else market
    if market not in {"JP", "TW"}:
        raise DataError("UNSUPPORTED_MARKET")
    dynamic = market == "JP" and (discover is True or (discover is None and symbols is None and bundle is None)
        or (discover is None and bundle is not None and bundle.get("discovery_required") is True))
    if discover is True and symbols is not None:
        raise DataError("DISCOVERY_AND_MANUAL_SYMBOLS_ARE_EXCLUSIVE")
    if dynamic and agents and max_calls < 3:
        raise DataError("DISCOVERY_REQUIRES_SEARCH_PLUS_ONE_REVIEW_PAIR_BUDGET")
    if type(scan_limit) is not int or not 1 <= scan_limit <= 100:
        raise DataError("SCAN_LIMIT_MUST_BE_1_TO_100")
    if symbols is None and not dynamic:
        symbols = strict_json_loads((ROOT / "config" / ("universe_jp.json" if market == "JP" else "universe_tw.json")).read_text(encoding="utf-8"))["symbols"]
    run_id = datetime.now(JST).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:10]
    target = output_root / run_id
    cursor_path = Path(cursor_path or db_path.parent / "discovery_cursor.json")
    cohort_cursor = None
    coverage = {}
    fresh_bundle = bundle is None
    fill_history = None
    try:
        if market == "JP" and (fresh_bundle or portfolio_path is not None):
            from .portfolio import load_fills, snapshot as portfolio_snapshot
            fill_history = load_fills(portfolio_path or db_path.parent / "manual_fills.json")
            held_now = portfolio_snapshot(fill_history, datetime.now(JST).isoformat())["positions"]
        else:
            held_now = {}
        if dynamic:
            from .universe import load_universe, select_cohort
            if bundle is None:
                if universe is None and universe_loader is None and universe_source == "fixed":
                    from .fixed_universe import load_fixed_universe, DEFAULT_FILE
                    universe = load_fixed_universe(universe_path or DEFAULT_FILE)
                elif universe is None:
                    if universe_source not in {"fixed", "jpx"}:
                        raise DataError("UNSUPPORTED_UNIVERSE_SOURCE")
                    universe = (universe_loader or load_universe)(db_path.parent / "jpx-universe", timeout=timeout)
                if universe.get("protocol") == "jp-fixed-universe-v1":
                    symbols = universe["symbols"][:scan_limit]
                    coverage = {"universe_count": len(universe["symbols"]), "scanned_count": len(symbols),
                        "cohort_symbols": list(symbols), "scope": "CONFIGURABLE_FIXED_POOL_SEARCH",
                        "source_type": "USER_FIXED_POOL", "broker_eligibility": universe["broker_eligibility"]}
                else:
                    with pointer_lock(cursor_path.with_suffix(".lock")):
                        cohort_cursor = _cursor_read(cursor_path, universe["universe_id"], universe["symbols"])
                        cursor_revision = (strict_json_loads(cursor_path.read_text(encoding="utf-8")).get("revision", 0)
                            if cursor_path.exists() else 0)
                    symbols, next_cursor = select_cohort(universe, limit=scan_limit, cursor=cohort_cursor)
                    coverage = {"universe_count": len(universe["items"]), "scanned_count": len(symbols),
                        "cursor": cohort_cursor, "next_cursor": next_cursor, "cohort_symbols": list(symbols),
                        "cursor_revision": cursor_revision,
                        "cursor_policy": "COMPARE_AND_SWAP_AFTER_PUBLICATION",
                        "scope": "ROTATING_BOUNDED_COHORT_NOT_FULL_MARKET_PER_RUN"}
            else:
                universe = universe or bundle.get("source_universe")
                if not isinstance(universe, dict):
                    raise DataError("DISCOVERY_UNIVERSE_REQUIRED")
                coverage = deepcopy(bundle.get("discovery", {}).get("coverage", {})) or {
                    "universe_count": len(universe["symbols"]), "scanned_count": len(bundle["securities"]),
                    "cohort_symbols": [s["symbol"] for s in bundle["securities"]],
                    "scope": "SAVED_COHORT_REVIEW_NO_CURSOR_ADVANCE"}
        if bundle is None:
            extra_holdings = [s for s in held_now if s not in symbols]
            symbols = [*symbols, *extra_holdings]
            if len(symbols) > 100:
                raise DataError("UNIVERSE_AND_HOLDINGS_EXCEED_FETCH_CAP")
            if dynamic:
                coverage["portfolio_only_symbols"] = extra_holdings
            if market == "JP":
                from .japan import build_snapshot as build_jp_snapshot
                selected_builder = build_jp_snapshot
            else:
                selected_builder = build_tw_snapshot
            bundle = (builder or selected_builder)(symbols, cache_dir or ROOT / "local_data" / ("yahoo-cache" if market == "JP" else "twse-cache"),
                months=months, timeout=timeout, progress=progress)
            if bundle.get("market", "TW") != market:
                raise DataError("PROVIDER_MARKET_MISMATCH")
        else:
            bundle = deepcopy(bundle)
        if dynamic:
            bundle.pop("discovery", None)
            bundle["discovery_required"] = True
            bundle["source_universe"] = deepcopy(universe)
            bundle["universe_version"] = universe["universe_id"] + ":" + digest([s["symbol"] for s in bundle["securities"]])[:12]
        if market == "JP":
            from .patterns import load_registry
            bundle["pattern_registry"] = load_registry(bundle.get("pattern_registry"))
            if fresh_bundle:
                from .communication import load_policy as load_communication
                bundle["communication_policy"] = load_communication()
                from .opportunity import load_policy as load_opportunity
                bundle["opportunity_policy"] = load_opportunity()
            if fill_history is not None:
                from .portfolio import snapshot as portfolio_snapshot
                bundle["portfolio_snapshot"] = portfolio_snapshot(fill_history, bundle["as_of"])
            # Existing programmatic fixtures/saved inputs remain opt-in; the live
            # CLI explicitly enables this by default. Freeze before SEARCH IDs.
            if public_information is not None:
                from .public_information import load_policy
                policy = load_policy()
                policy["enabled"] = bool(public_information)
                bundle["public_information_policy"] = policy
        if bundle.get("synthetic") is not False or bundle.get("observation_only") is not True:
            raise DataError("OBSERVE_REQUIRES_REAL_OBSERVATION_SNAPSHOT")
        agent_status = "NOT_REQUESTED"
        report = run(bundle, rules=rules)
        ai = None
        if agents and (researcher is None or reviewer is None or (dynamic and search_provider is None)):
            ai = openai_config(settings, model=model)
            if ai["ready"]:
                researcher = researcher or OpenAIProvider(ai["model"], max_output_tokens=4000)
                reviewer = reviewer or OpenAIProvider(ai["reviewer_model"], max_output_tokens=4000)
                if dynamic:
                    from .discovery import OpenAISearchProvider
                    search_provider = search_provider or OpenAISearchProvider(ai["model"])
        if dynamic:
            from .discovery import make_search_packet, run_search
            search = None
            if agents and search_provider is not None and researcher is not None and reviewer is not None:
                if len({search_provider.mode, researcher.mode, reviewer.mode}) != 1:
                    raise DataError("MIXED_SEARCH_AND_REVIEW_PROVIDER_MODES_FORBIDDEN")
                packet = make_search_packet(report, universe, max_selected=min(40, max_packets, (max_calls - 1) // 2))
                search = run_search(packet, search_provider)
            bundle["discovery"] = {"mode": "AGENT_SEARCH" if agents else "NUMERIC_ONLY",
                "coverage": coverage, "universe": {k: v for k, v in universe.items() if k not in {"items", "symbols"}},
                "search": search, "max_total_calls": max_calls, "total_call_count": search["call_count"] if search else 0}
            report = run(bundle, rules=rules)
            if agents:
                if search is None:
                    agent_status = "CONFIGURATION_REQUIRED"
                elif search["status"] == "FAILED" and not bundle.get("portfolio_snapshot", {}).get("positions"):
                    agent_status = "SEARCH_FAILED"
                elif not search["selection"] and not bundle.get("portfolio_snapshot", {}).get("positions"):
                    agent_status = "SEARCH_" + search["status"]
                else:
                    selected = [s["symbol"] for s in search["selection"]]
                    remaining = max_calls - search["call_count"]
                    report = run_desk(bundle, researcher=researcher, reviewer=reviewer, rules=rules,
                        config=DeskConfig(max_packets, remaining), selected_symbols=selected)
                    agent_status = _agent_status(report)
                    if search["status"] == "FAILED":
                        agent_status = "SEARCH_FAILED"
                    bundle["discovery"]["total_call_count"] += report["desk"]["call_count"]
                    if bundle["discovery"]["total_call_count"] > max_calls:
                        raise DataError("DISCOVERY_CALL_BUDGET_EXCEEDED")
                    report["discovery"] = deepcopy(bundle["discovery"])
        elif agents:
            if researcher is not None and reviewer is not None:
                report = run_desk(bundle, researcher=researcher, reviewer=reviewer, rules=rules, config=config)
            else:
                ai = openai_config(settings, model=model)
                if ai["ready"]:
                    report = run_desk(bundle, researcher=OpenAIProvider(ai["model"], max_output_tokens=4000),
                        reviewer=OpenAIProvider(ai["reviewer_model"], max_output_tokens=4000), rules=rules, config=config)
                else:
                    agent_status = "CONFIGURATION_REQUIRED"
            if "desk" in report:
                agent_status = _agent_status(report)
        # Each successful ledger transaction references this immutable run directory.
        report["observation"] = {"run_id": run_id, "agent_status": agent_status,
            "report_directory": str(target.resolve()), "status": "OBSERVATION_READY"}
        rejected = sum(s["data_status"] == "REJECTED" for s in report["securities"])
        data_ready = report["collection"].get("status") in {"OK", "COMPLETE"} and not rejected
        source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted((ROOT / "support_scout").glob("*.py"))}
        code = 0
        def publish_before_commit(summary):
            nonlocal code
            report["ledger"] = summary
            if market == "JP":
                from .decision import attach_decisions
                attach_decisions(report, bundle)
                if "opportunity_policy" in report:
                    from .opportunity import attach_opportunities
                    attach_opportunities(report, bundle)
                if "portfolio_snapshot" in report:
                    from .portfolio import attach_manual_actions
                    attach_manual_actions(report)
            degraded = not data_ready or (agents and agent_status in {"FAILED", "SEARCH_FAILED", "CONFIGURATION_REQUIRED"}) or bool(summary.get("errors"))
            report["observation"]["status"] = "DEGRADED" if degraded else "OBSERVATION_READY"
            code = 2 if rejected == len(report["securities"]) else 3 if degraded else 0
            manifest = {"run_id": run_id, "effective_as_of": report["as_of"], "rules": asdict(rules),
                "rules_hash": digest(asdict(rules)), "universe_version": report["universe_version"],
                "input_digest": digest(bundle), "snapshot_id": report["snapshot_id"],
                "source_code_hashes": source_hashes, "exit_code": code, "ledger_path": str(db_path.resolve()),
                "agents_requested": agents, "agent_status": agent_status, "execution_enabled": False}
            manifest["market"] = market
            values = {"input.json": bundle, "report.json": report, "manifest.json": manifest,
                "rules.json": asdict(rules), "agent_tasks.json": report["agent_packets"],
                "reviews.json": {"reviews": report.get("reviews", [])}, "desk_audit.json": report.get("desk", {})}
            if market == "JP":
                values["patterns.json"] = bundle["pattern_registry"]
                if "communication_policy" in bundle:
                    values["communication_policy.json"] = bundle["communication_policy"]
                if "opportunity_policy" in bundle:
                    values["opportunity_policy.json"] = bundle["opportunity_policy"]
                if "portfolio_snapshot" in bundle:
                    values["portfolio_snapshot.json"] = bundle["portfolio_snapshot"]
                manifest["pattern_registry_digest"] = digest(bundle["pattern_registry"])
                if "public_information_policy" in bundle:
                    values["public_information_policy.json"] = bundle["public_information_policy"]
                    manifest["public_information_policy_digest"] = digest(bundle["public_information_policy"])
            if dynamic:
                values["universe.json"] = universe
                values["search_audit.json"] = report["discovery"]
                manifest["discovery_required"] = True
                manifest["total_call_count"] = report["discovery"]["total_call_count"]
            files = {name: json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n" for name, value in values.items()}
            files.update({"report.md": render_markdown(report), "report.html": render_html(report, bundle)})
            publish_run(target, files)
        report["ledger"] = record_observation(report, db_path, before_commit=publish_before_commit)
        atomic_json(target / "committed.json", {"run_id": run_id, "ledger_committed": True})
        if (cohort_cursor is not None and agent_status not in {"FAILED", "SEARCH_FAILED", "CONFIGURATION_REQUIRED"}
                and any(s["data_status"] != "REJECTED" for s in report["securities"])):
            _cursor_commit(cursor_path, universe["universe_id"], cohort_cursor, coverage["next_cursor"],
                symbols=universe["symbols"], universe_as_of=universe["as_of"], expected_revision=coverage["cursor_revision"])
        pointer = {"run_id": run_id, "status": report["observation"]["status"],
            "as_of": report["as_of"], "latest_session": report["collection"].get("actual_latest_session"),
            "report": str((target / "report.html").resolve()), "exit_code": code}
        publish_latest(output_root / "latest.json", pointer)
        atomic_json(output_root / "latest_attempt.json", pointer)
        if fresh_bundle and market == "JP":
            from .notifications import after_report
            after_report(report, target, config_path=db_path.parent / "email_settings.json",
                db_path=db_path.parent / "notifications.sqlite3")
        return report, target, code
    except Exception as exc:
        error = str(exc) if isinstance(exc, DataError) else type(exc).__name__
        atomic_json(output_root / "failures" / (run_id + ".json"),
            {"run_id": run_id, "status": "FAILED", "error": error, "occurred_at": datetime.now(JST).isoformat()})
        # Do not relabel any previous successful report as current.
        atomic_json(output_root / "latest_attempt.json", {"run_id": run_id, "status": "FAILED", "error": error})
        raise DataError("OBSERVATION_FAILED:" + error) from None
