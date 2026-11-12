"""Reconstruct remaining AI entry plans from recorded fills, without guessing legacy plans."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
from config import TAIPEI, now
from market_calendar import twse_closed_reason


def remaining_plans(events, held_qty):
    lots = []
    def sequence_time(row):
        stamp = datetime.fromisoformat(row['at'])
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

    for event in sorted(events, key=sequence_time):
        qty = int(event['quantity'])
        if qty <= 0:
            continue
        if event['side'] == 'BUY':
            stamp = datetime.fromisoformat(event['at'])
            entry_date = stamp.astimezone(TAIPEI).date().isoformat() if stamp.tzinfo else stamp.date().isoformat()
            lots.append(dict(quantity=qty, original_quantity=qty, plan=event.get('plan'),
                             entry_date=entry_date, date_basis=event['date_basis']))
        elif event['side'] == 'SELL':
            remaining = qty
            while lots and remaining:
                take = min(lots[0]['quantity'], remaining)
                lots[0]['quantity'] -= take
                remaining -= take
                if lots[0]['quantity'] == 0:
                    lots.pop(0)
    actual = sum(row['quantity'] for row in lots)
    if actual > held_qty:
        return dict(available=False, lots=[], reason='LEDGER_QUANTITY_MISMATCH')
    if actual < held_qty:
        lots.insert(0, dict(quantity=held_qty-actual, plan=None, entry_date=None,
                            date_basis='LEGACY_OR_UNMATCHED_HOLDING'))
    return dict(available=bool(lots) and all(row['plan'] for row in lots),
                lots=lots, allocation='FIFO_APPROXIMATION',
                note='部分賣出依先進先出配對；委託日期不代表券商逐筆成交時刻。')


def saved_entry_plans(analysis_path, run_ids):
    """Exact historical run/symbol lookup; never infer plans from later advice."""
    if analysis_path is None or not Path(analysis_path).is_file() or not run_ids:
        return {}
    plans = {}
    try:
        with closing(sqlite3.connect(Path(analysis_path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
            for run_id in set(run_ids):
                row = db.execute('SELECT decision_json FROM selection_analyses WHERE run_id=?', (run_id,)).fetchone()
                if not row:
                    continue
                try:
                    decisions = json.loads(row[0] or '{}').get('decisions', [])
                except (ValueError, TypeError, AttributeError):
                    continue
                seen = set()
                for d in decisions:
                    if not isinstance(d, dict) or not isinstance(d.get('symbol'), str):
                        continue
                    key = run_id, d['symbol']
                    if key in seen:
                        plans.pop(key, None)
                        continue
                    seen.add(key)
                    if d.get('decision') != 'BUY':
                        continue
                    if (any(type(d.get(k)) not in (int, float) or not 0 < d[k] < float('inf')
                            for k in ('suggested_price', 'target_price', 'invalidation_price'))
                            or not d['invalidation_price'] < d['suggested_price'] < d['target_price']
                            or type(d.get('holding_days')) is not int or not 1 <= d['holding_days'] <= 22):
                        continue
                    plans[key] = {k: d.get(k) for k in ('strategy_id', 'target_price', 'invalidation_price',
                                                      'holding_days', 'reason', 'suggested_price')}
                    plans[key].update(source='SAVED_ANALYSIS_MATCHED_TO_FILLED_BUY', source_run_id=run_id,
                        note='原始買進計畫；不代表依今日策略重新批准，舊投射目標不自動成為今日合法目標。')
    except (sqlite3.Error, OSError):
        # Missing old archives leave an explicitly unknown plan, not a guessed
        # substitute, and must not stop independent current-account risk checks.
        return {}
    return plans


def plan_review_status(context, as_of):
    """Expose elapsed completed sessions; expiry asks for review, not a SELL."""
    for lot in context.get('lots', []):
        if not lot.get('plan') or not lot.get('entry_date'):
            continue
        start = datetime.fromisoformat(lot['entry_date']).date()
        complete = 0
        day = start + timedelta(days=1)
        while day < as_of:
            complete += twse_closed_reason(day) is None
            day += timedelta(days=1)
        horizon = lot['plan'].get('holding_days')
        lot['review_status'] = dict(as_of_date=as_of.isoformat(),
            completed_sessions_after_entry=complete, original_holding_days=horizon,
            review_due=isinstance(horizon, int) and complete >= horizon,
            note='不計買進日與尚未完成的本交易日；到期須重新評估，不自動賣出。')
    return context


def live_plans(path, quantities, analysis_path=None, as_of=None):
    output = {}
    path = Path(path).resolve()
    with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)) as db:
        has_adoptions = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_ai_adoptions'").fetchone()
        # Older orders predate buy_plan persistence. Their original saved
        # analysis is usable only after matching an actual filled BUY below.
        has_run_id = any(row[1] == 'run_id' for row in db.execute('PRAGMA table_info(execution_attempts)'))
        originals = saved_entry_plans(analysis_path, [row[0] for row in db.execute(
            "SELECT DISTINCT run_id FROM execution_attempts WHERE side='BUY'")]) if has_run_id else {}
        for symbol, held in quantities.items():
            events = []
            for run_id, side, at, raw in db.execute(
                    ('SELECT run_id' if has_run_id else "SELECT NULL") + ",side,started_at,detail_json FROM execution_attempts "
                    "WHERE symbol=? AND side IN ('BUY','SELL') ORDER BY started_at", (symbol,)):
                try:
                    detail = json.loads(raw or '{}')
                    qty = int(detail.get('filled_quantity') or 0)
                except (ValueError, TypeError):
                    continue
                if qty > 0:
                    events.append(dict(side=side, at=at, quantity=qty,
                                       plan=(detail.get('buy_plan') or originals.get((run_id, symbol))) if side == 'BUY' else None,
                                       date_basis='FILLED_ORDER_SUBMISSION_DATE'))
            if has_adoptions:
                for qty, at in db.execute(
                        'SELECT qty,adopted_at FROM manual_ai_adoptions WHERE symbol=?', (symbol,)):
                    events.append(dict(side='BUY', at=at, quantity=qty, plan=None,
                                       date_basis='MANUAL_PURCHASE_ENTRY_DATE'))
            output[symbol] = plan_review_status(remaining_plans(events, held), as_of or now().date())
    return output
