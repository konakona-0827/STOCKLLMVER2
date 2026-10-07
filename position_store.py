"""User-configured analysis positions, never a broker or fill ledger."""
import json
import math
import re
import sqlite3
from config import now


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE IF NOT EXISTS positions (
        symbol TEXT PRIMARY KEY, user_qty INTEGER NOT NULL, ai_managed_qty INTEGER NOT NULL,
        user_average_cost REAL, ai_average_cost REAL, updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS position_events (
        id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, occurred_at TEXT NOT NULL,
        event_type TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL);
    ''')
    return db


def load(path):
    db = connect(path)
    try:
        return [dict(r) for r in db.execute('SELECT * FROM positions ORDER BY symbol')]
    finally:
        db.close()


def save_manual(path, symbol, user_qty, ai_managed_qty, user_average_cost=None, ai_average_cost=None):
    if not isinstance(symbol, str) or not re.fullmatch(r'(?:[0-9]{4,6}|[0-9]{4,5}[A-Z])', symbol):
        raise ValueError('標的代碼須為有效的 4–6 字元上市證券代碼')
    for qty in (user_qty, ai_managed_qty):
        if type(qty) is not int or qty < 0:
            raise ValueError('股數須為非負整數')
    for cost in (user_average_cost, ai_average_cost):
        if cost is not None and (type(cost) not in (int, float) or not math.isfinite(cost) or cost <= 0):
            raise ValueError('成本須留白或為正數')
    p = dict(symbol=symbol, user_qty=user_qty, ai_managed_qty=ai_managed_qty,
             user_average_cost=user_average_cost, ai_average_cost=ai_average_cost,
             updated_at=now().isoformat())
    db = connect(path)
    try:
        with db:
            old = db.execute('SELECT * FROM positions WHERE symbol=?', (symbol,)).fetchone()
            db.execute('INSERT OR REPLACE INTO positions VALUES(?,?,?,?,?,?)', tuple(p.values()))
            db.execute('INSERT INTO position_events(symbol,occurred_at,event_type,before_json,after_json) VALUES(?,?,?,?,?)',
                       (symbol, p['updated_at'], 'MANUAL_CONFIGURATION', json.dumps(dict(old)) if old else None, json.dumps(p)))
    finally:
        db.close()
    return p


def context(p, price):
    p = dict(p)
    user, ai = p['user_qty'], p['ai_managed_qty']
    total = user + ai
    p.update(total_qty=total, ai_managed_ratio=ai/total if total else 0,
             current_price=price, source='USER_CONFIGURED_NOT_BROKER_VERIFIED')
    def pnl(qty, cost):
        if qty == 0:
            return 0
        return (price-cost)*qty if price is not None and cost is not None else None
    up, ap = pnl(user, p['user_average_cost']), pnl(ai, p['ai_average_cost'])
    p.update(ai_unrealized_pnl=ap, total_unrealized_pnl=up+ap if up is not None and ap is not None else None,
             ai_unrealized_pnl_pct=ap/(p['ai_average_cost']*ai) if ap is not None and ai and p['ai_average_cost'] else None)
    return p


def empty(symbol):
    return dict(symbol=symbol, user_qty=0, ai_managed_qty=0, user_average_cost=None,
                ai_average_cost=None, updated_at=None)
