"""Hourly, read-only broker and SQLite health report for the GUI runtime."""
from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from config import TAIPEI
from .account_guard import query_buying_power, query_inventory, realtime_cash_qty
from .auto_advice_executor import LiveExecutionConfig


def _append_csv(path: Path, fields: tuple[str, ...], row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    first = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8-sig" if first else "utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        if first:
            writer.writeheader()
        writer.writerow(row)


def _read_execution_db(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"execution database missing: {path}")
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)) as con:
        integrity = con.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite quick_check: {integrity}")
        positions = {str(symbol).upper(): int(qty) for symbol, qty in con.execute(
            "SELECT symbol, qty FROM ai_positions"
        )}
        unresolved = [dict(zip(("decision_id", "symbol", "status", "started_at"), row))
                      for row in con.execute(
                          """SELECT decision_id, symbol, status, started_at
                             FROM execution_attempts
                             WHERE finished_at IS NULL OR status IN
                                   ('ACKNOWLEDGED','PARTIALLY_FILLED','UNCONFIRMED')"""
                      )]
    return positions, unresolved


def _read_analysis_positions(path: Path) -> dict[str, dict[str, int]]:
    if not path.exists():
        return {}
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro",
                                 uri=True, timeout=10)) as con:
        return {str(symbol).upper(): dict(user_qty=int(user_qty),
                                          configured_ai_qty=int(ai_qty))
                for symbol, user_qty, ai_qty in con.execute(
                    "SELECT symbol, user_qty, ai_managed_qty FROM positions"
                )}


def run_health_check(project_root: str | Path, *, session=None,
                     connection_error: str | None = None) -> dict:
    root = Path(project_root)
    output = root / "data" / "health"
    checked_at = datetime.now(TAIPEI).isoformat(timespec="seconds")
    cfg = LiveExecutionConfig.load(root / "config" / "execution_live.json")
    report = dict(checked_at=checked_at, status="OK", broker_balance_twd=None,
                  minimum_balance_twd=str(cfg.min_available_to_buy_twd),
                  balance_below_floor=False, db_ai_positions={},
                  analysis_positions={},
                  inventory_comparison=[], unresolved_attempts=[], errors=[],
                  last_scan_status=None, last_scan_at=None,
                  last_execution_status=None, analysis_running=False)

    analysis_dir = root / "data" / "analysis"
    report["analysis_running"] = (analysis_dir / "runtime.lock").exists()
    for path, key, nested in (
        (analysis_dir / "latest_dashboard.json", "last_scan_status", True),
        (root / "data" / "execution" / "latest_live_execution.json",
         "last_execution_status", False),
    ):
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                source = saved.get("manifest", {}) if nested else saved
                report[key] = source.get("status")
                if nested:
                    report["last_scan_at"] = source.get("finished_at")
            except Exception as exc:
                report["errors"].append(
                    f"log {path.name}: {type(exc).__name__}: {exc}"
                )
    if (report["last_scan_status"] == "ERROR"
            or report["last_execution_status"] == "ERROR"):
        report["status"] = "WARN"

    try:
        positions, unresolved = _read_execution_db(
            root / "data" / "execution" / "live_execution.sqlite3"
        )
        report["db_ai_positions"] = positions
        report["unresolved_attempts"] = unresolved
        if unresolved:
            report["status"] = "WARN"
    except Exception as exc:
        report["errors"].append(f"database: {type(exc).__name__}: {exc}")

    try:
        report["analysis_positions"] = _read_analysis_positions(
            analysis_dir / "market_history.sqlite3"
        )
    except Exception as exc:
        report["errors"].append(f"analysis positions: {type(exc).__name__}: {exc}")

    inventory = None
    if session is None:
        report["errors"].append(connection_error or "broker session unavailable")
    else:
        try:
            health = session.ensure_ready()
            report["session_action"] = health.action
        except Exception as exc:
            report["errors"].append(f"session: {type(exc).__name__}: {exc}")
        else:
            try:
                buying_power = query_buying_power(session)
                available = buying_power.available_to_buy_twd
                report["broker_balance_twd"] = str(available)
                report["balance_below_floor"] = available < cfg.min_available_to_buy_twd
            except Exception as exc:
                report["errors"].append(f"balance: {type(exc).__name__}: {exc}")
            try:
                inventory = query_inventory(session)
            except Exception as exc:
                report["errors"].append(f"inventory: {type(exc).__name__}: {exc}")

    if inventory is not None and not any(x.startswith("database:") for x in report["errors"]):
        broker_symbols = {row.symbol for row in inventory if row.inventory_type == "T"}
        for symbol in sorted(set(report["db_ai_positions"]) | broker_symbols |
                             set(report["analysis_positions"])):
            ai_qty = report["db_ai_positions"].get(symbol, 0)
            broker_qty = realtime_cash_qty(inventory, symbol)
            configured = report["analysis_positions"].get(symbol, {})
            configured_total = (
                configured["user_qty"] + configured["configured_ai_qty"]
                if configured else None
            )
            expected_total = ai_qty + configured.get("user_qty", 0)
            difference_broker_minus_expected = broker_qty - expected_total
            if ai_qty > broker_qty:
                comparison_status = "AI_EXCEEDS_BROKER"
            elif configured and broker_qty < expected_total:
                comparison_status = "CONFIGURED_EXCEEDS_BROKER"
            elif broker_qty > expected_total:
                comparison_status = "BROKER_EXCESS_UNATTRIBUTED"
            else:
                comparison_status = "MATCH"
            comparison = dict(symbol=symbol, ai_qty=ai_qty,
                              broker_cash_qty=broker_qty,
                              configured_user_qty=configured.get("user_qty"),
                              configured_ai_qty=configured.get("configured_ai_qty"),
                              configured_total_qty=configured_total,
                              difference_broker_minus_configured=(
                                  broker_qty - configured_total
                                  if configured_total is not None else None
                              ),
                              difference_broker_minus_ai=broker_qty - ai_qty,
                              status=comparison_status)
            report["inventory_comparison"].append(comparison)
            if comparison_status in ("AI_EXCEEDS_BROKER", "CONFIGURED_EXCEEDS_BROKER"):
                report["status"] = "ALERT"
            elif comparison_status == "BROKER_EXCESS_UNATTRIBUTED" and report["status"] == "OK":
                report["status"] = "WARN"
            try:
                _append_csv(output / "inventory_reconciliation.csv",
                            ("checked_at", "symbol", "ai_qty", "broker_cash_qty",
                             "configured_user_qty", "configured_ai_qty",
                             "configured_total_qty", "expected_total_qty",
                             "difference_broker_minus_expected",
                             "difference_broker_minus_configured",
                             "difference_broker_minus_ai", "status"),
                            dict(checked_at=checked_at, **comparison))
            except OSError as exc:
                report["errors"].append(f"inventory CSV: {type(exc).__name__}: {exc}")

    if report["errors"]:
        report["status"] = "ERROR"
    elif report["balance_below_floor"]:
        report["status"] = "ALERT"

    output.mkdir(parents=True, exist_ok=True)
    line = (f"{checked_at} HEALTH {report['status']} "
            f"balance={report['broker_balance_twd']} "
            f"inventory_checked={inventory is not None} "
            f"ai_exceeds_broker={sum(x['status'] == 'AI_EXCEEDS_BROKER' for x in report['inventory_comparison'])} "
            f"unattributed_broker={sum(x['status'] == 'BROKER_EXCESS_UNATTRIBUTED' for x in report['inventory_comparison'])} "
            f"unresolved={len(report['unresolved_attempts'])} "
            f"errors={len(report['errors'])}")
    with (output / "runtime.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    try:
        _append_csv(output / "health_checks.csv",
                    ("checked_at", "status", "analysis_running", "last_scan_status",
                     "last_scan_at", "last_execution_status", "broker_balance_twd", "minimum_balance_twd",
                     "balance_below_floor", "db_ai_position_count",
                     "analysis_position_count", "inventory_checked",
                     "ai_exceeds_broker_count", "unattributed_broker_count",
                     "unresolved_attempt_count", "errors"),
                    dict(checked_at=checked_at, status=report["status"],
                         analysis_running=report["analysis_running"],
                         last_scan_status=report["last_scan_status"],
                         last_scan_at=report["last_scan_at"],
                         last_execution_status=report["last_execution_status"],
                         broker_balance_twd=report["broker_balance_twd"],
                         minimum_balance_twd=report["minimum_balance_twd"],
                         balance_below_floor=report["balance_below_floor"],
                         db_ai_position_count=len(report["db_ai_positions"]),
                         analysis_position_count=len(report["analysis_positions"]),
                         inventory_checked=inventory is not None,
                         ai_exceeds_broker_count=sum(x["status"] == "AI_EXCEEDS_BROKER"
                                                     for x in report["inventory_comparison"]),
                         unattributed_broker_count=sum(x["status"] == "BROKER_EXCESS_UNATTRIBUTED"
                                                       for x in report["inventory_comparison"]),
                         unresolved_attempt_count=len(report["unresolved_attempts"]),
                         errors="; ".join(report["errors"])))
    except OSError as exc:
        report["errors"].append(f"health CSV: {type(exc).__name__}: {exc}")
        report["status"] = "ERROR"
    latest = output / "latest_health.json"
    temp = output / "latest_health.json.tmp"
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(latest)
    print(line, flush=True)
    return report
