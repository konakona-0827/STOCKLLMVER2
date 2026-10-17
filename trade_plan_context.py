"""Reconstruct remaining AI entry plans from recorded fills, without guessing legacy plans."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from config import TAIPEI


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
            lots.append(dict(quantity=qty, plan=event.get('plan'),
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


def live_plans(path, quantities):
    output = {}
    path = Path(path).resolve()
    with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)) as db:
        has_adoptions = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_ai_adoptions'").fetchone()
        for symbol, held in quantities.items():
            events = []
            for side, at, raw in db.execute(
                    "SELECT side,started_at,detail_json FROM execution_attempts "
                    "WHERE symbol=? AND side IN ('BUY','SELL') ORDER BY started_at", (symbol,)):
                try:
                    detail = json.loads(raw or '{}')
                    qty = int(detail.get('filled_quantity') or 0)
                except (ValueError, TypeError):
                    continue
                if qty > 0:
                    events.append(dict(side=side, at=at, quantity=qty,
                                       plan=detail.get('buy_plan') if side == 'BUY' else None,
                                       date_basis='FILLED_ORDER_SUBMISSION_DATE'))
            if has_adoptions:
                for qty, at in db.execute(
                        'SELECT qty,adopted_at FROM manual_ai_adoptions WHERE symbol=?', (symbol,)):
                    events.append(dict(side='BUY', at=at, quantity=qty, plan=None,
                                       date_basis='MANUAL_PURCHASE_ENTRY_DATE'))
            output[symbol] = remaining_plans(events, held)
    return output
