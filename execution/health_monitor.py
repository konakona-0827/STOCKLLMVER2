"""Hourly, read-only broker and SQLite health report for the GUI runtime."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
import time
import uuid
from decimal import Decimal
from contextlib import closing
from datetime import datetime
from pathlib import Path

from config import TAIPEI
from .account_guard import (query_buying_power, query_inventory,
                            query_settlement_dues, realtime_cash_qty)
from .auto_advice_executor import LiveExecutionConfig
from .capital_crosscheck import parse_reply_row


_broker_sync_lock = threading.Lock()


def _append_csv(path: Path, fields: tuple[str, ...], row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    first = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8-sig" if first else "utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        if first:
            writer.writeheader()
        writer.writerow(row)


def _publish_latest_health(path: Path, report: dict) -> str | None:
    """Retry transient Windows/OneDrive sharing locks without losing broker data."""
    body = json.dumps(report, ensure_ascii=False, indent=2)
    last_error = None
    for delay in (0, 0.1, 0.25, 0.5, 1, 2):
        if delay:
            time.sleep(delay)
        temp = path.with_name(
            f'.{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp')
        try:
            temp.write_text(body, encoding="utf-8")
            temp.replace(path)
            return None
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (None, 5, 32):
                raise
            last_error = f'{type(exc).__name__}: WinError {getattr(exc, "winerror", None)}'
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    return last_error


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


def _read_order_keys(path: Path) -> list[dict]:
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)) as con:
        return [dict(decision_id=decision_id, symbol=symbol, side=side,
                     status=status,
                     detail=json.loads(detail_json or "{}"))
                for decision_id, symbol, side, status, detail_json in con.execute(
                    """SELECT decision_id,symbol,side,status,detail_json FROM execution_attempts
                       WHERE status IN ('FILLED','PARTIALLY_FILLED','ACKNOWLEDGED','UNCONFIRMED',
                                        'CANCELLED','PARTIALLY_FILLED_CANCELLED')
                       ORDER BY started_at DESC LIMIT 100""")]


def current_buy_block_reasons(report: dict) -> list[str]:
    """Explain why new BUY orders are paused without calling broker cash zero."""
    reasons = []
    if report.get("errors"):
        reasons.append("BROKER_CHECK_ERROR")
    if report.get("broker_balance_twd") is None:
        reasons.append("BALANCE_UNVERIFIED")
    if report.get("inventory_checked") is not True:
        reasons.append("INVENTORY_UNVERIFIED")
    if report.get("balance_below_floor"):
        reasons.append("BELOW_MINIMUM_BALANCE")
    if report.get("pending_buy_reserve_error"):
        reasons.append("PENDING_RESERVE_UNVERIFIED")
    if report.get("ai_committed_capital_error"):
        reasons.append("AI_CAPITAL_UNVERIFIED")
    try:
        pending = Decimal(str(report["pending_buy_reserve_twd"]))
        invested = Decimal(str(report["ai_committed_capital_twd"]))
        strategy = Decimal(str(report["strategy_cap_twd"]))
        daily_limit = Decimal(str(report["daily_buy_limit_twd"]))
        daily_used = Decimal(str(report["daily_buy_reserved_twd"]))
        if strategy - invested - pending <= 0:
            reasons.append("AI_CAPITAL_LIMIT_REACHED")
        if daily_limit - daily_used <= 0:
            reasons.append("DAILY_BUY_LIMIT_REACHED")
        if (report.get("broker_balance_twd") is not None and
                Decimal(str(report["broker_balance_twd"])) -
                Decimal(str(report["minimum_balance_twd"])) - pending <= 0):
            reasons.append("NO_BROKER_HEADROOM_AFTER_RESERVE")
    except (KeyError, TypeError, ValueError, ArithmeticError):
        pass
    if any(row.get("status") != "MATCH"
           for row in report.get("inventory_comparison") or []):
        reasons.append("INVENTORY_MISMATCH")
    checks = report.get("broker_order_reconciliation") or []
    if any(row.get("status") == "FILL_MISMATCH" for row in checks):
        reasons.append("BROKER_FILL_LEDGER_MISMATCH")
    if any(row.get("status") not in
           ("FILL_MATCH", "ARCHIVED_BROKER_FILL_VERIFIED", "FILL_MISMATCH",
            "NO_FILL_CONFIRMED", "NO_FILL_YET_VERIFIED")
           for row in checks):
        reasons.append("BROKER_FILL_UNVERIFIED")
    if report.get("last_execution_status") == "ERROR":
        reasons.append("PRIOR_EXECUTION_ERROR")
    if (not reasons and report.get("status") != "OK"
            and not (report.get("status") == "WARN"
                     and (report.get("last_scan_status") == "ERROR"
                          or report.get("unresolved_attempts")))):
        reasons.append("OTHER_HEALTH_WARNING")
    return reasons


def _pending_buy_reservation(project_root: Path, report: dict,
                             cfg: LiveExecutionConfig) -> tuple[list[dict], Decimal]:
    """Hold the worst-case remaining limit value of every open BUY order."""
    db_path = project_root / "data" / "execution" / "live_execution.sqlite3"
    checks = {row.get("decision_id"): row for row in
              report.get("broker_order_reconciliation") or []}
    with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro",
                                 uri=True, timeout=10)) as con:
        rows = con.execute(
            """SELECT decision_id,symbol,status,detail_json FROM execution_attempts
               WHERE side='BUY' AND (finished_at IS NULL OR status IN
                     ('ACKNOWLEDGED','PARTIALLY_FILLED','UNCONFIRMED'))"""
        ).fetchall()
    pending, total = [], Decimal("0")
    for decision_id, symbol, status, detail_json in rows:
        detail = json.loads(detail_json or "{}")
        check = checks.get(decision_id)
        if (check is None or not check.get("broker_replay_complete")
                or check.get("status") not in
                   ("FILL_MATCH", "ARCHIVED_BROKER_FILL_VERIFIED",
                    "NO_FILL_YET_VERIFIED")
                or check.get("broker_event_status") in
                   ("NO_MATCHING_BROKER_EVENT", "PRIOR_FILL_VERIFIED_REPLAY_NOT_RETURNED")
                or detail.get("send_return_code") != 0
                or str(detail.get("seq13")) != str(check.get("seq13"))):
            raise RuntimeError(f"{symbol} {decision_id}: pending BUY broker state unverified")
        remaining = int(check.get("unconfirmed_qty") or 0)
        ordered = int(detail.get("quantity") or 0)
        filled = int(check.get("broker_verified_filled_qty") or 0)
        if ordered <= 0 or not 0 <= filled <= ordered or remaining != ordered - filled:
            raise RuntimeError(f"{symbol} {decision_id}: pending BUY quantity conflicts")
        price = Decimal(str(detail.get("limit_price")))
        if not price.is_finite() or price <= 0:
            raise RuntimeError(f"{symbol} {decision_id}: pending BUY limit price missing")
        gross = price * remaining
        fee = (max(cfg.min_buy_fee_reserve_twd,
                   gross * cfg.buy_cash_buffer_rate) if remaining else Decimal("0"))
        amount = gross + fee
        pending.append(dict(decision_id=decision_id, symbol=symbol,
                            remaining_qty=remaining, limit_price_twd=str(price),
                            gross_twd=str(gross), fee_buffer_twd=str(fee),
                            reserve_twd=str(amount)))
        total += amount
    return pending, total


def _ai_committed_capital(project_root: Path,
                          cfg: LiveExecutionConfig) -> tuple[Decimal, list[dict], bool]:
    """Rebuild remaining AI acquisition cost, releasing cost basis on SELL fills."""
    db_path = project_root / "data" / "execution" / "live_execution.sqlite3"
    with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro",
                                 uri=True, timeout=10)) as con:
        attempts = con.execute(
            """SELECT symbol,side,detail_json FROM execution_attempts
               ORDER BY started_at,rowid""").fetchall()
        formal = {str(symbol): int(qty) for symbol, qty in con.execute(
            "SELECT symbol,qty FROM ai_positions")}
    holdings: dict[str, tuple[int, Decimal]] = {}
    limit_price_estimated = False
    for symbol, side, detail_json in attempts:
        detail = json.loads(detail_json or "{}")
        filled = int(detail.get("filled_quantity") or 0)
        if filled <= 0:
            continue
        qty, cost = holdings.get(symbol, (0, Decimal("0")))
        if side == "BUY":
            fills = detail.get("broker_replay_fill_details") or []
            if fills:
                if sum(int(row["quantity"]) for row in fills) != filled:
                    raise RuntimeError(f"{symbol}: broker fill prices do not cover quantity")
                gross = sum(Decimal(str(row["price_twd"])) * int(row["quantity"])
                            for row in fills)
            else:
                # A limit BUY cannot fill above its limit. This overstates
                # unknown execution cost and keeps the capital cap conservative.
                gross = Decimal(str(detail.get("limit_price"))) * filled
                limit_price_estimated = True
            if not gross.is_finite() or gross <= 0:
                raise RuntimeError(f"{symbol}: AI acquisition cost is unverified")
            fee = max(cfg.min_buy_fee_reserve_twd,
                      gross * cfg.buy_cash_buffer_rate)
            holdings[symbol] = (qty + filled, cost + gross + fee)
        elif side == "SELL":
            if filled > qty:
                raise RuntimeError(f"{symbol}: AI SELL exceeds reconstructed holdings")
            remaining = qty - filled
            holdings[symbol] = (remaining,
                                cost * Decimal(remaining) / Decimal(qty))
        else:
            raise RuntimeError(f"{symbol}: unknown execution side")
    if {symbol: qty for symbol, (qty, _) in holdings.items() if qty > 0} != {
            symbol: qty for symbol, qty in formal.items() if qty > 0}:
        raise RuntimeError("AI holdings and acquisition history disagree")
    items = [dict(symbol=symbol, qty=qty, committed_cost_twd=str(cost))
             for symbol, (qty, cost) in sorted(holdings.items()) if qty > 0]
    return sum((Decimal(row["committed_cost_twd"]) for row in items),
               Decimal("0")), items, limit_price_estimated


def current_buy_sources_verified(report: dict) -> bool:
    """Ignore a past analysis failure, but require current broker/ledger evidence."""
    return not current_buy_block_reasons(report)


def reconcile_confirmed_broker_events(project_root: str | Path, report: dict) -> dict:
    """Apply exact-SEQ13 broker fills and cancellations to the formal ledger."""
    outcome = dict(updated=[], errors=[])
    if report.get("errors") or report.get("inventory_checked") is not True:
        return outcome
    comparisons = {row["symbol"]: row for row in
                   report.get("inventory_comparison") or []}
    from .execution_store import ExecutionStore
    store = ExecutionStore(
        Path(project_root) / "data" / "execution" / "live_execution.sqlite3",
        must_exist=True,
    )
    for check in report.get("broker_order_reconciliation") or []:
        if check.get("side") not in ("BUY", "SELL"):
            continue
        symbol = check.get("symbol")
        inventory = comparisons.get(symbol)
        try:
            if not check.get("broker_replay_complete") or inventory is None:
                if (check.get("status") == "FILL_MISMATCH"
                        or check.get("broker_event_counts", {}).get("cancel")):
                    raise RuntimeError("complete broker replay or inventory is missing")
                continue
            broker_qty = int(check.get("broker_replay_filled_qty") or 0)
            ledger_qty = int(check.get("ledger_filled_qty") or 0)
            if check.get("status") == "FILL_MISMATCH" and broker_qty > ledger_qty:
                fill_change = store.reconcile_broker_fill(
                    check["decision_id"], check["seq13"], broker_qty,
                    check.get("broker_replay_fill_details") or [],
                    int(inventory["broker_cash_qty"]),
                    int(inventory.get("configured_user_qty") or 0),
                )
                if fill_change.get("changed"):
                    outcome["updated"].append(fill_change)
                ledger_qty = broker_qty
            verified_qty = int(check.get("broker_verified_filled_qty") or 0)
            if (check.get("broker_event_counts", {}).get("cancel")
                    and check.get("status") in
                        ("FILL_MATCH", "ARCHIVED_BROKER_FILL_VERIFIED",
                         "NO_FILL_CONFIRMED", "FILL_MISMATCH")
                    and verified_qty == ledger_qty
                    and ledger_qty < int(check.get("ordered_qty") or 0)):
                cancel_change = store.reconcile_broker_cancel(
                    check["decision_id"], check["seq13"], ledger_qty)
                if cancel_change.get("changed"):
                    outcome["updated"].append(cancel_change)
        except Exception as exc:
            outcome["errors"].append(dict(
                decision_id=check.get("decision_id"),
                reason=f"{type(exc).__name__}: {exc}"))
    return outcome


def reconcile_confirmed_buy_fills(project_root: str | Path, report: dict) -> dict:
    """Compatibility alias for the broker event reconciliation entry point."""
    return reconcile_confirmed_broker_events(project_root, report)


def sync_broker_state(project_root: str | Path, runtime=None, *, enrich=None,
                      return_report: bool = False):
    """Serialize broker reads, exact-event ledger updates, and saved UI snapshot."""
    root = Path(project_root)
    from .broker_runtime import get_broker_runtime
    with _broker_sync_lock:
        broker = runtime or get_broker_runtime(root)
        health = broker.health()
        reconciliation = reconcile_confirmed_broker_events(root, health)
        if any(row.get("changed") for row in reconciliation["updated"]):
            # A second read reflects the newly committed formal ledger. The
            # broker sometimes rejects an immediate repeat inventory query.
            time.sleep(2)
            health = broker.health()
        health["reconciled_fills"] = reconciliation["updated"]
        health["reconciliation_errors"] = reconciliation["errors"]
        if reconciliation["errors"]:
            health["errors"].extend(
                "broker reconciliation: " + str(row["decision_id"]) + ": "
                + row["reason"] for row in reconciliation["errors"])
            health["status"] = "ERROR"
        if enrich is not None:
            enrich(health, broker)
        snapshot = persist_refresh_snapshot(root, health)
        return (health, snapshot) if return_report else snapshot


def persist_refresh_snapshot(project_root: str | Path, report: dict) -> dict:
    """Archive broker evidence and formal order records without inventing fills."""
    db_path = Path(project_root) / "data" / "execution" / "live_execution.sqlite3"
    if not db_path.is_file():
        raise FileNotFoundError(f"execution database missing: {db_path}")
    with closing(sqlite3.connect(db_path, timeout=10)) as con:
        con.execute("BEGIN IMMEDIATE")
        previous = {}
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_refresh_snapshots'").fetchone():
            row = con.execute("SELECT payload_json FROM broker_refresh_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                previous = json.loads(row[0])
        counts = {status: count for status, count in con.execute(
            "SELECT status, COUNT(*) FROM execution_attempts GROUP BY status")}
        attempts = [dict(zip(
            ("decision_id", "run_id", "symbol", "side", "started_at", "finished_at", "status", "detail"),
            (*row[:7], json.loads(row[7] or "{}")),
        )) for row in con.execute(
            """SELECT decision_id, run_id, symbol, side, started_at, finished_at,
                      status, detail_json FROM execution_attempts
               ORDER BY started_at DESC LIMIT 100""")]
        broker_inventory_verified = (
            report.get("status") != "ERROR"
            and not report.get("errors")
            and report.get("broker_balance_twd") is not None
            and report.get("inventory_checked") is True
        )
        buy_block_reasons = current_buy_block_reasons(report)
        buy_sources_verified = not buy_block_reasons
        snapshot = {
            "checked_at": report.get("checked_at"),
            "status": report.get("status"),
            "last_scan_status": report.get("last_scan_status"),
            "last_execution_status": report.get("last_execution_status"),
            "latest_health_write_warning": report.get("latest_health_write_warning"),
            "broker_balance_twd": report.get("broker_balance_twd"),
            "broker_one_account_balance_twd": report.get("broker_one_account_balance_twd"),
            "broker_withdrawable_twd": report.get("broker_withdrawable_twd"),
            "broker_settlement_dues": report.get("broker_settlement_dues", []),
            "settlement_query_error": report.get("settlement_query_error"),
            "minimum_balance_twd": report.get("minimum_balance_twd"),
            "balance_below_floor": report.get("balance_below_floor", False),
            "inventory_comparison": report.get("inventory_comparison", []),
            "position_quotes": report.get("position_quotes", previous.get("position_quotes", [])),
            "position_quote_errors": report.get("position_quote_errors", previous.get("position_quote_errors", {})),
            "position_quotes_checked_at": report.get("position_quotes_checked_at", previous.get("position_quotes_checked_at")),
            "ai_positions": report.get("db_ai_positions", {}),
            "unresolved_attempts": report.get("unresolved_attempts", []),
            "execution_attempt_counts": counts,
            "execution_attempts": attempts,
            "broker_order_reconciliation": report.get("broker_order_reconciliation", []),
            "reconciled_fills": report.get("reconciled_fills", []),
            "reconciliation_errors": report.get("reconciliation_errors", []),
            "daily_buy_limit_twd": report.get("daily_buy_limit_twd"),
            "daily_buy_reserved_twd": report.get("daily_buy_reserved_twd"),
            "pending_buy_orders": report.get("pending_buy_orders", []),
            "pending_buy_reserve_twd": report.get("pending_buy_reserve_twd"),
            "pending_buy_reserve_error": report.get("pending_buy_reserve_error"),
            "ai_committed_capital_twd": report.get("ai_committed_capital_twd"),
            "ai_committed_positions": report.get("ai_committed_positions", []),
            "ai_committed_capital_estimated": report.get("ai_committed_capital_estimated", False),
            "ai_committed_capital_error": report.get("ai_committed_capital_error"),
            "strategy_cap_twd": report.get("strategy_cap_twd"),
            "broker_inventory_verified": broker_inventory_verified,
            "buy_sources_verified": buy_sources_verified,
            "buy_block_reasons": buy_block_reasons,
            "trade_records_source": "FORMAL_EXECUTION_LEDGER; broker inventory verifies aggregate holdings, not individual fill prices",
            "errors": report.get("errors", []),
            "session_action": report.get("session_action"),
        }
        try:
            balance = snapshot["broker_balance_twd"]
            daily_limit = snapshot["daily_buy_limit_twd"]
            daily_reserved = snapshot["daily_buy_reserved_twd"]
            strategy_cap = snapshot["strategy_cap_twd"]
            pending_reserve = snapshot["pending_buy_reserve_twd"]
            invested = snapshot["ai_committed_capital_twd"]
            if None in (balance, daily_limit, daily_reserved,
                        strategy_cap, pending_reserve, invested):
                raise ValueError("budget inputs are incomplete")
            after_floor = max(Decimal("0"), Decimal(str(balance)) -
                              Decimal(str(snapshot["minimum_balance_twd"] or 0)))
            after_pending = max(Decimal("0"), after_floor -
                                Decimal(str(pending_reserve)))
            daily_remaining = max(Decimal("0"), Decimal(str(daily_limit)) -
                                  Decimal(str(daily_reserved)))
            strategy_remaining = max(
                Decimal("0"), Decimal(str(strategy_cap)) -
                Decimal(str(invested)) - Decimal(str(pending_reserve)))
            snapshot["broker_remaining_after_minimum_twd"] = str(after_floor)
            snapshot["broker_remaining_after_pending_twd"] = str(after_pending)
            snapshot["daily_buy_remaining_twd"] = str(daily_remaining)
            snapshot["strategy_remaining_twd"] = str(strategy_remaining)
            calculated_budget = min(after_pending, strategy_remaining,
                                    daily_remaining)
            snapshot["precheck_buy_headroom_twd"] = str(calculated_budget)
            snapshot["current_buy_budget_twd"] = str(
                calculated_budget if buy_sources_verified else Decimal("0"))
            snapshot["budget_status"] = (
                "READY" if buy_sources_verified
                and Decimal(snapshot["current_buy_budget_twd"]) > 0 else "BLOCKED")
        except (TypeError, ValueError, ArithmeticError):
            snapshot["broker_remaining_after_minimum_twd"] = None
            snapshot["broker_remaining_after_pending_twd"] = None
            snapshot["strategy_remaining_twd"] = None
            snapshot["daily_buy_remaining_twd"] = None
            snapshot["current_buy_budget_twd"] = None
            snapshot["budget_status"] = "UNVERIFIED"
        con.execute("""CREATE TABLE IF NOT EXISTS broker_refresh_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, checked_at TEXT NOT NULL,
            status TEXT NOT NULL, payload_json TEXT NOT NULL)""")
        con.execute("INSERT INTO broker_refresh_snapshots(checked_at,status,payload_json) VALUES(?,?,?)",
                    (snapshot["checked_at"], snapshot["status"], json.dumps(snapshot, ensure_ascii=False)))
        con.commit()
    return snapshot


def latest_refresh_snapshot(project_root: str | Path) -> dict | None:
    db_path = Path(project_root) / "data" / "execution" / "live_execution.sqlite3"
    if not db_path.is_file():
        return None
    with closing(sqlite3.connect(db_path, timeout=10)) as con:
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_refresh_snapshots'").fetchone()
        if not exists:
            return None
        row = con.execute("SELECT payload_json FROM broker_refresh_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def run_health_check(project_root: str | Path, *, session=None,
                     connection_error: str | None = None) -> dict:
    root = Path(project_root)
    output = root / "data" / "health"
    checked_at = datetime.now(TAIPEI).isoformat(timespec="seconds")
    cfg = LiveExecutionConfig.load(root / "config" / "execution_live.json")
    report = dict(checked_at=checked_at, status="OK", broker_balance_twd=None,
                  broker_one_account_balance_twd=None,
                  broker_withdrawable_twd=None,
                  broker_settlement_dues=[], settlement_query_error=None,
                  minimum_balance_twd=str(cfg.min_available_to_buy_twd),
                  daily_buy_limit_twd=str(cfg.max_daily_buy_twd),
                  daily_buy_reserved_twd=None, strategy_cap_twd=None,
                  pending_buy_orders=[], pending_buy_reserve_twd=None,
                  pending_buy_reserve_error=None,
                  ai_committed_capital_twd=None, ai_committed_positions=[],
                  ai_committed_capital_estimated=False,
                  ai_committed_capital_error=None,
                  balance_below_floor=False, db_ai_positions={},
                  analysis_positions={},
                  inventory_comparison=[], broker_order_reconciliation=[],
                  unresolved_attempts=[], errors=[],
                  last_scan_status=None, last_scan_at=None,
                  last_execution_status=None, analysis_running=False)

    analysis_dir = root / "data" / "analysis"
    try:
        from .execution_store import ExecutionStore
        report["daily_buy_reserved_twd"] = str(ExecutionStore(
            root / "data" / "execution" / "live_execution.sqlite3", must_exist=True
        ).daily_reserved_buy_twd())
    except Exception as exc:
        report["errors"].append(f"BUY budget ledger: {type(exc).__name__}: {exc}")
    try:
        paper_db = root / "data" / "analysis" / "market_history.sqlite3"
        with closing(sqlite3.connect(f"file:{paper_db.as_posix()}?mode=ro", uri=True, timeout=10)) as con:
            row = con.execute("SELECT capital_limit_cents FROM paper_account WHERE account_id=1").fetchone()
        report["strategy_cap_twd"] = None if row is None else str(Decimal(row[0])/100)
    except Exception as exc:
        report["errors"].append(f"strategy budget: {type(exc).__name__}: {exc}")
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
    broker_session_ready = False
    if session is None:
        report["errors"].append(connection_error or "broker session unavailable")
    else:
        try:
            health = session.ensure_ready()
            report["session_action"] = health.action
            broker_session_ready = True
        except Exception as exc:
            report["errors"].append(f"session: {type(exc).__name__}: {exc}")
        else:
            try:
                buying_power = query_buying_power(session)
                available = buying_power.available_to_buy_twd
                report["broker_balance_twd"] = str(available)
                report["broker_one_account_balance_twd"] = str(buying_power.balance_twd)
                report["broker_withdrawable_twd"] = str(buying_power.withdrawable_twd)
                report["balance_below_floor"] = available < cfg.min_available_to_buy_twd
            except Exception as exc:
                report["errors"].append(f"balance: {type(exc).__name__}: {exc}")
            try:
                dues = query_settlement_dues(session)
                report["broker_settlement_dues"] = [dict(
                    trade_date=row.trade_date,
                    settlement_date=row.settlement_date,
                    net_twd=str(row.net_twd),
                ) for row in dues]
            except Exception as exc:
                # GetBalance's buying power remains the broker's trading
                # authority even when this optional settlement view fails.
                report["settlement_query_error"] = f"{type(exc).__name__}: {exc}"
            try:
                inventory = query_inventory(session)
            except Exception as exc:
                report["errors"].append(f"inventory: {type(exc).__name__}: {exc}")

    report["inventory_checked"] = inventory is not None
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

    if broker_session_ready and not any(x.startswith("database:") for x in report["errors"]):
        try:
            for attempt in _read_order_keys(root / "data" / "execution" / "live_execution.sqlite3"):
                detail = attempt["detail"]
                seq13 = detail.get("seq13")
                if not seq13:
                    report["broker_order_reconciliation"].append(dict(
                        decision_id=attempt["decision_id"], status="NO_BROKER_SEQ_UNVERIFIED"))
                    if report["status"] == "OK":
                        report["status"] = "WARN"
                    continue
                cross = session.crosscheck_seq13(str(seq13), include_report9=True)
                recorded_qty = int(detail.get("filled_quantity") or 0)
                broker_qty = cross.filled_quantity
                ordered_qty = int(detail.get("quantity") or 0)
                accepted_count = len(cross.accepted_rows)
                fill_event_count = len(cross.fill_rows)
                cancel_count = len(cross.cancel_rows)
                archived_cancel = (attempt["status"] in
                                   ("CANCELLED", "PARTIALLY_FILLED_CANCELLED")
                                   and detail.get("broker_cancel_seq13") == str(seq13))
                has_cancel = bool(cancel_count or archived_cancel)
                if broker_qty >= ordered_qty > 0:
                    event_status = "FULLY_FILLED"
                elif broker_qty > 0:
                    event_status = ("PARTIAL_FILL_REMAINDER_CANCELLED" if has_cancel
                                    else "PARTIALLY_FILLED_REMAINDER_UNCONFIRMED")
                elif has_cancel:
                    event_status = "CANCELLED_NO_FILL"
                elif accepted_count:
                    event_status = "ACKNOWLEDGED_NO_FILL_EVENT"
                else:
                    event_status = "NO_MATCHING_BROKER_EVENT"
                fills = []
                for raw in cross.fill_rows:
                    parsed = parse_reply_row(raw)
                    if parsed.quantity is not None and parsed.quantity > 0:
                        fills.append(dict(
                            quantity=parsed.quantity,
                            price_twd=(str(parsed.price) if parsed.price is not None
                                       and parsed.price > 0 else None)))
                gross_twd = (str(sum(
                    Decimal(row["price_twd"]) * row["quantity"] for row in fills))
                    if fills and all(row["price_twd"] is not None for row in fills)
                    else None)
                if broker_qty and broker_qty == recorded_qty:
                    verification = "FILL_MATCH"
                elif broker_qty and broker_qty != recorded_qty:
                    verification = "FILL_MISMATCH"
                    report["status"] = "ALERT"
                elif (has_cancel and (cross.replay_complete or archived_cancel)
                      and recorded_qty == 0):
                    verification = "NO_FILL_CONFIRMED"
                elif (accepted_count and cross.replay_complete
                      and recorded_qty == 0 and broker_qty == 0):
                    verification = "NO_FILL_YET_VERIFIED"
                elif (recorded_qty > 0
                      and detail.get("broker_reconciled_seq13") == str(seq13)
                      and int(detail.get("broker_reconciled_qty") or 0) == recorded_qty):
                    verification = "ARCHIVED_BROKER_FILL_VERIFIED"
                    if not has_cancel:
                        event_status = "PRIOR_FILL_VERIFIED_REPLAY_NOT_RETURNED"
                else:
                    verification = "FILL_UNVERIFIED"
                    if report["status"] == "OK":
                        report["status"] = "WARN"
                verified_qty = broker_qty
                if verification == "ARCHIVED_BROKER_FILL_VERIFIED":
                    verified_qty = recorded_qty
                    fills = detail.get("broker_replay_fill_details") or []
                    gross_twd = detail.get("broker_replay_fill_gross_twd")
                if has_cancel and 0 < verified_qty < ordered_qty:
                    event_status = "PARTIAL_FILL_REMAINDER_CANCELLED"
                report["broker_order_reconciliation"].append(dict(
                    decision_id=attempt["decision_id"],
                    symbol=attempt["symbol"], side=attempt["side"], seq13=str(seq13),
                    ledger_status=attempt["status"], ledger_filled_qty=recorded_qty,
                    ordered_qty=ordered_qty,
                    broker_replay_filled_qty=broker_qty,
                    broker_verified_filled_qty=verified_qty,
                    unconfirmed_qty=(0 if has_cancel else
                                     max(0, ordered_qty-verified_qty)),
                    cancelled_qty=(max(0, ordered_qty-verified_qty)
                                   if has_cancel else 0),
                    broker_event_status=event_status,
                    broker_event_counts=dict(ack=accepted_count, fill=fill_event_count,
                                             cancel=cancel_count),
                    broker_replay_complete=cross.replay_complete,
                    broker_replay_fill_details=fills,
                    broker_replay_fill_gross_twd=gross_twd,
                    broker_report9_contains_seq=cross.report9_contains_seq,
                    status=verification))
        except Exception as exc:
            report["errors"].append(f"order reconciliation: {type(exc).__name__}: {exc}")

    try:
        pending, reserved = _pending_buy_reservation(root, report, cfg)
        report["pending_buy_orders"] = pending
        report["pending_buy_reserve_twd"] = str(reserved)
    except Exception as exc:
        report["pending_buy_reserve_error"] = f"{type(exc).__name__}: {exc}"
    try:
        invested, items, estimated = _ai_committed_capital(root, cfg)
        report["ai_committed_capital_twd"] = str(invested)
        report["ai_committed_positions"] = items
        report["ai_committed_capital_estimated"] = estimated
    except Exception as exc:
        report["ai_committed_capital_error"] = f"{type(exc).__name__}: {exc}"

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
    write_error = _publish_latest_health(output / "latest_health.json", report)
    if write_error:
        report["latest_health_write_warning"] = write_error
        try:
            with (output / "runtime.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{checked_at} HEALTH_FILE_WRITE_WARN {write_error}\n")
        except OSError:
            pass
    print(line, flush=True)
    return report
