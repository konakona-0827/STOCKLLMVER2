"""Collect end-of-day snapshots, run bounded research and publish decision evidence."""
from __future__ import annotations

from dataclasses import asdict
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
from .storage import atomic_json, publish_run, publish_latest
from .twse import build_snapshot as build_tw_snapshot

JST = timezone(timedelta(hours=9))


def observe(*, symbols=None, months=8, cache_dir=None, output_root=None, db_path=None,
            settings_path=DEFAULT_SETTINGS, agents=None, model=None, max_packets=5, max_calls=10,
            timeout=15, bundle=None, builder=None, researcher=None, reviewer=None, rules=None, progress=None,
            market="JP"):
    settings = load_settings(Path(settings_path))
    agents = settings.get("run_agents", False) if agents is None else agents
    rules = rules or Rules()
    config = DeskConfig(max_packets, max_calls)
    output_root = Path(output_root or ROOT / "output" / "observations")
    db_path = Path(db_path or ROOT / "local_data" / "observations.sqlite3")
    market = bundle.get("market", "TW") if bundle is not None else market
    if market not in {"JP", "TW"}:
        raise DataError("UNSUPPORTED_MARKET")
    if symbols is None:
        symbols = strict_json_loads((ROOT / "config" / ("universe_jp.json" if market == "JP" else "universe_tw.json")).read_text(encoding="utf-8"))["symbols"]
    run_id = datetime.now(JST).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:10]
    target = output_root / run_id
    try:
        if bundle is None:
            if market == "JP":
                from .japan import build_snapshot as build_jp_snapshot
                selected_builder = build_jp_snapshot
            else:
                selected_builder = build_tw_snapshot
            bundle = (builder or selected_builder)(symbols, cache_dir or ROOT / "local_data" / ("yahoo-cache" if market == "JP" else "twse-cache"),
                months=months, timeout=timeout, progress=progress)
            if bundle.get("market", "TW") != market:
                raise DataError("PROVIDER_MARKET_MISMATCH")
        if bundle.get("synthetic") is not False or bundle.get("observation_only") is not True:
            raise DataError("OBSERVE_REQUIRES_REAL_OBSERVATION_SNAPSHOT")
        agent_status = "NOT_REQUESTED"
        report = run(bundle, rules=rules)
        if agents:
            if researcher is not None and reviewer is not None:
                report = run_desk(bundle, researcher=researcher, reviewer=reviewer, rules=rules, config=config)
            else:
                ai = openai_config(settings, model=model)
                if ai["ready"]:
                    report = run_desk(bundle, researcher=OpenAIProvider(ai["model"]),
                        reviewer=OpenAIProvider(ai["reviewer_model"]), rules=rules, config=config)
                else:
                    agent_status = "CONFIGURATION_REQUIRED"
            if "desk" in report:
                agent_status = ("FAILED" if report["desk"]["error_count"] else "NO_ELIGIBLE_PACKETS" if not report["desk"]["call_count"]
                    else "PARTIAL_BUDGET" if report["desk"]["deferred_symbols"] else "COMPLETED")
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
            degraded = not data_ready or (agents and agent_status in {"FAILED", "CONFIGURATION_REQUIRED"}) or bool(summary.get("errors"))
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
            files = {name: json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n" for name, value in values.items()}
            files.update({"report.md": render_markdown(report), "report.html": render_html(report, bundle)})
            publish_run(target, files)
        report["ledger"] = record_observation(report, db_path, before_commit=publish_before_commit)
        atomic_json(target / "committed.json", {"run_id": run_id, "ledger_committed": True})
        pointer = {"run_id": run_id, "status": report["observation"]["status"],
            "as_of": report["as_of"], "latest_session": report["collection"].get("actual_latest_session"),
            "report": str((target / "report.html").resolve()), "exit_code": code}
        publish_latest(output_root / "latest.json", pointer)
        atomic_json(output_root / "latest_attempt.json", pointer)
        return report, target, code
    except Exception as exc:
        error = str(exc) if isinstance(exc, DataError) else type(exc).__name__
        atomic_json(output_root / "failures" / (run_id + ".json"),
            {"run_id": run_id, "status": "FAILED", "error": error, "occurred_at": datetime.now(JST).isoformat()})
        # Do not relabel any previous successful report as current.
        atomic_json(output_root / "latest_attempt.json", {"run_id": run_id, "status": "FAILED", "error": error})
        raise DataError("OBSERVATION_FAILED:" + error) from None
