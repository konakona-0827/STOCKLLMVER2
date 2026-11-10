"""Broker-checked position updates and audited Dashboard ownership changes."""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3

from config import TAIPEI
import position_store
from .execution_store import ExecutionStore
from .auto_advice_executor import LiveExecutionConfig


def _record_position(con, before, after, event_type, evidence=None):
    con.execute("""INSERT INTO analysis.positions
        (symbol,user_qty,ai_managed_qty,user_average_cost,ai_average_cost,updated_at)
        VALUES(:symbol,:user_qty,:ai_managed_qty,:user_average_cost,:ai_average_cost,:updated_at)
        ON CONFLICT(symbol) DO UPDATE SET user_qty=excluded.user_qty,
        ai_managed_qty=excluded.ai_managed_qty,user_average_cost=excluded.user_average_cost,
        ai_average_cost=excluded.ai_average_cost,updated_at=excluded.updated_at""", after)
    con.execute("""INSERT INTO analysis.position_events
        (symbol,occurred_at,event_type,before_json,after_json) VALUES(?,?,?,?,?)""",
        (after['symbol'], after['updated_at'], event_type, json.dumps(before),
         json.dumps({**after, **(evidence or {})})))


def reconcile_broker_inventory(project_root, report):
    """Bring local cash holdings in line with a complete broker snapshot."""
    differences = [row for row in report.get('inventory_comparison', [])
                   if row['status'] != 'MATCH']
    if (not differences or report.get('errors') or report.get('status') == 'ERROR'
            or report.get('inventory_checked') is not True):
        return []
    checked = datetime.fromisoformat(report['checked_at'])
    if checked.tzinfo is None:
        return []

    from .health_monitor import VERIFIED_BROKER_ORDER_STATUSES
    unresolved = {row.get('symbol') for row in report.get('broker_order_reconciliation', [])
                  if row['status'] not in VERIFIED_BROKER_ORDER_STATUSES}
    if None in unresolved:
        return []

    root = Path(project_root)
    analysis_db = root / 'data' / 'analysis' / 'market_history.sqlite3'
    store = ExecutionStore(root / 'data' / 'execution' / 'live_execution.sqlite3',
                           must_exist=True)
    costs = {row['symbol']: row for row in report.get('ai_committed_positions', [])}
    adjusted = []
    with store._connect() as con:
        con.execute('ATTACH DATABASE ? AS analysis', (str(analysis_db.resolve()),))
        con.execute('BEGIN IMMEDIATE')
        at = datetime.now(TAIPEI)
        if not 0 <= (at - checked).total_seconds() <= 120:
            return []
        stamp = at.isoformat()
        con.row_factory = sqlite3.Row
        for row in differences:
            symbol = row['symbol']
            if symbol in unresolved:
                continue
            activity = con.execute("""SELECT 1 FROM execution_attempts WHERE symbol=?
                AND (finished_at IS NULL OR status IN
                     ('STARTED','SEND_PENDING','SUBMITTED','ACKNOWLEDGED',
                      'PARTIALLY_FILLED','UNCONFIRMED')
                     OR julianday(started_at)>=julianday(?)
                     OR julianday(finished_at)>=julianday(?)) LIMIT 1""",
                (symbol, report['checked_at'], report['checked_at'])).fetchone()
            if activity:
                continue
            ai = con.execute('SELECT qty,updated_at FROM ai_positions WHERE symbol=?',
                             (symbol,)).fetchone()
            old = con.execute('SELECT * FROM analysis.positions WHERE symbol=?',
                              (symbol,)).fetchone()
            before = dict(old) if old is not None else None
            old_user = old['user_qty'] if old is not None else None
            previous_ai = ai['qty'] if ai is not None else 0
            if (previous_ai != row['ai_qty'] or old_user != row['configured_user_qty']
                    or (ai is not None and datetime.fromisoformat(ai['updated_at']) >= checked)):
                continue

            broker_qty = row['broker_cash_qty']
            if not re.fullmatch(r'(?:[0-9]{4,6}|[0-9]{4,5}[A-Z])', symbol):
                continue
            # Reserve known manual shares first when the untagged broker total falls.
            ai_qty = min(previous_ai, max(0, broker_qty - (old_user or 0)))
            user_qty = broker_qty - ai_qty
            if ai_qty != previous_ai:
                evidence = costs.get(symbol, {})
                if report.get('ai_committed_capital_error') or evidence.get('qty') != previous_ai:
                    continue
                cost = Decimal(evidence['committed_cost_twd'])
                if not cost.is_finite() or cost <= 0:
                    continue
                cost = cost * ai_qty / previous_ai
                con.execute('UPDATE ai_positions SET qty=?,updated_at=? WHERE symbol=?',
                            (ai_qty, stamp, symbol))
                con.execute("""INSERT INTO ai_position_adjustments
                    (symbol,previous_qty,qty,committed_cost_twd,adjusted_at,
                     broker_cash_qty,broker_checked_at) VALUES(?,?,?,?,?,?,?)""",
                    (symbol, previous_ai, ai_qty, str(cost), stamp, broker_qty, checked.isoformat()))

            after = dict(symbol=symbol, user_qty=user_qty, ai_managed_qty=ai_qty,
                         user_average_cost=(old['user_average_cost']
                                            if old is not None and old_user == user_qty else None),
                         ai_average_cost=(old['ai_average_cost'] if old is not None
                                          and old['ai_managed_qty'] == ai_qty else None),
                         updated_at=stamp)
            evidence = dict(broker_cash_qty=broker_qty, broker_checked_at=checked.isoformat())
            _record_position(con, before, after, 'BROKER_INVENTORY_RECONCILIATION', evidence)
            adjusted.append(dict(symbol=symbol, previous_user_qty=old_user or 0,
                                 previous_ai_qty=previous_ai, user_qty=user_qty, ai_qty=ai_qty,
                                 **evidence))
    return adjusted


def save_ai_holding_configuration(project_root, analysis_db, symbol, qty, snapshot,
                                  *, unit_cost_twd=None, at=None):
    root, analysis_db = Path(project_root), Path(analysis_db)
    if not isinstance(symbol, str) or not re.fullmatch(r'(?:[0-9]{4,6}|[0-9]{4,5}[A-Z])', symbol):
        raise ValueError('股票代號格式錯誤')
    if type(qty) is not int or qty < 0:
        raise ValueError('AI 管理股數須為非負整數；0 表示禁止 AI 買賣')
    at = at or datetime.now(TAIPEI)
    checked = datetime.fromisoformat(snapshot.get('checked_at') or '')
    if (checked.tzinfo is None or at.tzinfo is None
            or not 0 <= (at-checked).total_seconds() <= 120
            or snapshot.get('broker_inventory_verified') is not True
            or snapshot.get('errors')):
        raise ValueError('請先取得完整且在 120 秒內的券商庫存對帳')
    rows = {row['symbol']: row for row in snapshot.get('inventory_comparison') or []}
    broker_qty = int(rows.get(symbol, {}).get('broker_cash_qty') or 0)
    if qty > broker_qty:
        raise ValueError(f'AI 股數不得超過券商現股 {broker_qty} 股')
    for check in snapshot.get('broker_order_reconciliation') or []:
        if check.get('symbol') == symbol and check.get('status') in (
                'FILL_MISMATCH', 'FILL_UNVERIFIED', 'NO_BROKER_SEQ_UNVERIFIED'):
            raise ValueError('該股票仍有成交待核對，請完成對帳後再設定')
    cfg = LiveExecutionConfig.load(root/'config'/'execution_live.json')
    store = ExecutionStore(root/'data'/'execution'/'live_execution.sqlite3', must_exist=True)
    # Ensure the analysis schema exists before attaching both files to one transaction.
    position_store.connect(analysis_db).close()
    stamp = at.isoformat()
    with store._connect() as con:
        con.execute('ATTACH DATABASE ? AS analysis', (str(analysis_db.resolve()),))
        con.execute('BEGIN IMMEDIATE')
        pending = con.execute("""SELECT 1 FROM execution_attempts WHERE symbol=?
            AND status IN ('STARTED','SEND_PENDING','SUBMITTED','ACKNOWLEDGED',
                           'PARTIALLY_FILLED','UNCONFIRMED') LIMIT 1""", (symbol,)).fetchone()
        if pending:
            raise ValueError('該股票仍有未結委託，完成成交核對或委託失效後再設定')
        current = con.execute('SELECT qty FROM ai_positions WHERE symbol=?', (symbol,)).fetchone()
        previous = int(current[0]) if current else 0
        if previous != int(snapshot.get('ai_positions', {}).get(symbol, 0)):
            raise ValueError('正式 AI 股數已變動，請重新對帳後再設定')
        costs = {row['symbol']: row for row in snapshot.get('ai_committed_positions') or []}
        cost = Decimal(str(costs.get(symbol, {}).get('committed_cost_twd') or 0))
        if qty == 0:
            cost = Decimal('0')
        elif previous and (not cost.is_finite() or cost <= 0):
            raise ValueError('原 AI 持股成本未核實，請完成成本對帳後再設定')
        if 0 < qty < previous:
            cost = cost * Decimal(qty) / Decimal(previous)
        elif qty > previous:
            unit = Decimal(str(unit_cost_twd)) if unit_cost_twd is not None else Decimal('0')
            if not unit.is_finite() or unit <= 0:
                raise ValueError('增加 AI 管理股數時，請填入轉入股數的每股成本')
            gross = unit * (qty-previous)
            cost += gross + max(cfg.min_buy_fee_reserve_twd, gross*cfg.buy_cash_buffer_rate)
        old = con.execute('SELECT * FROM analysis.positions WHERE symbol=?', (symbol,)).fetchone()
        names = ('symbol', 'user_qty', 'ai_managed_qty', 'user_average_cost', 'ai_average_cost', 'updated_at')
        before = dict(zip(names, old)) if old else None
        after = dict(symbol=symbol, user_qty=broker_qty-qty, ai_managed_qty=qty,
                     user_average_cost=before.get('user_average_cost') if before else None,
                     ai_average_cost=None, updated_at=stamp)
        _record_position(con, before, after, 'LIVE_AI_CONFIGURATION')
        con.execute('''INSERT INTO ai_positions VALUES(?,?,?) ON CONFLICT(symbol)
            DO UPDATE SET qty=excluded.qty,updated_at=excluded.updated_at''', (symbol, qty, stamp))
        con.execute('''INSERT INTO ai_position_settings VALUES(?,?,?,?) ON CONFLICT(symbol)
            DO UPDATE SET enabled=excluded.enabled,configured_qty=excluded.configured_qty,
            updated_at=excluded.updated_at''', (symbol, int(qty > 0), qty, stamp))
        con.execute('''INSERT INTO ai_position_adjustments
            (symbol,previous_qty,qty,committed_cost_twd,adjusted_at,broker_cash_qty,broker_checked_at)
            VALUES(?,?,?,?,?,?,?)''', (symbol, previous, qty, str(cost), stamp, broker_qty, checked.isoformat()))
    return dict(symbol=symbol, ai_qty=qty, user_qty=broker_qty-qty, enabled=qty > 0)


def configure_ai_holding(project_root, analysis_db, symbol, qty, *, unit_cost_twd=None):
    from .health_monitor import _broker_sync_lock, sync_broker_state
    with _broker_sync_lock:
        snapshot = sync_broker_state(project_root)
        return save_ai_holding_configuration(project_root, analysis_db, symbol, qty,
                                             snapshot, unit_cost_twd=unit_cost_twd)
