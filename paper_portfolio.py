"""Persistent paper cash, positions and simulated fills; never submits broker orders."""
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
import json
import sqlite3
from pathlib import Path
from config import now

FEE_RATE = Decimal('0.001425')
MAX_ODDLOT_QTY = 999


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE IF NOT EXISTS paper_account (
        account_id INTEGER PRIMARY KEY CHECK(account_id=1),
        capital_limit_cents INTEGER NOT NULL, cash_cents INTEGER NOT NULL,
        updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS paper_positions (
        symbol TEXT PRIMARY KEY, qty INTEGER NOT NULL, cost_cents INTEGER NOT NULL,
        updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS paper_events (
        id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, event_type TEXT NOT NULL,
        amount_cents INTEGER NOT NULL, before_cents INTEGER NOT NULL,
        after_cents INTEGER NOT NULL, note TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
        symbol TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', side TEXT NOT NULL, qty INTEGER NOT NULL,
        price_cents INTEGER NOT NULL, gross_cents INTEGER NOT NULL,
        fee_twd INTEGER NOT NULL, cash_change_cents INTEGER NOT NULL,
        cost_basis_cents INTEGER NOT NULL, realized_pnl_cents INTEGER NOT NULL,
        reason TEXT NOT NULL);
    ''')
    trade_columns = {row[1] for row in db.execute('PRAGMA table_info(paper_trades)')}
    if 'name' not in trade_columns:
        db.execute("ALTER TABLE paper_trades ADD COLUMN name TEXT NOT NULL DEFAULT ''")
    return db


def account(path):
    db = connect(path)
    try:
        row = db.execute('SELECT * FROM paper_account WHERE account_id=1').fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def positions(path):
    db = connect(path)
    try:
        return [dict(r) for r in db.execute('SELECT * FROM paper_positions ORDER BY symbol')]
    finally:
        db.close()


def trades(path, limit=100):
    db = connect(path)
    try:
        return [dict(r) for r in db.execute(
            'SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?', (limit,))]
    finally:
        db.close()


def _twd_cents(amount):
    value = Decimal(str(amount))
    if not value.is_finite() or value <= 0:
        raise ValueError('模擬資金上限須為正數')
    return int((value * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def set_capital_limit(path, amount_twd):
    """Set initial paper capital or increase it; a higher limit credits only the delta."""
    target = _twd_cents(amount_twd)
    db = connect(path)
    try:
        with db:
            row = db.execute('SELECT * FROM paper_account WHERE account_id=1').fetchone()
            at = now().isoformat()
            if row is None:
                db.execute('INSERT INTO paper_account VALUES(1,?,?,?)', (target, target, at))
                db.execute('INSERT INTO paper_events(occurred_at,event_type,amount_cents,before_cents,after_cents,note) VALUES(?,?,?,?,?,?)',
                           (at, 'INITIAL_CAPITAL', target, 0, target, 'Initial simulated capital'))
            else:
                previous = row['capital_limit_cents']
                if target < previous:
                    raise ValueError('模擬資金上限只能維持或增加，不能調低；目前上限為 TWD ' + str(previous // 100))
                delta = target - previous
                if delta:
                    new_cash = row['cash_cents'] + delta
                    db.execute('UPDATE paper_account SET capital_limit_cents=?,cash_cents=?,updated_at=? WHERE account_id=1',
                               (target, new_cash, at))
                    db.execute('INSERT INTO paper_events(occurred_at,event_type,amount_cents,before_cents,after_cents,note) VALUES(?,?,?,?,?,?)',
                               (at, 'CAPITAL_TOP_UP', delta, row['cash_cents'], new_cash, 'Capital limit increased'))
        return account(path)
    finally:
        db.close()


def fee_twd(gross_cents):
    fee = int((Decimal(gross_cents) / 100 * FEE_RATE).to_integral_value(rounding=ROUND_FLOOR))
    return max(1, fee)


def price_cents(price):
    value = Decimal(str(price))
    if not value.is_finite() or value <= 0:
        raise ValueError('INVALID_SIMULATION_PRICE')
    return int((value * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def max_affordable_qty(cash_cents, price):
    cents = price_cents(price)
    lo, hi = 0, MAX_ODDLOT_QTY
    while lo < hi:
        mid = (lo + hi + 1) // 2
        gross = cents * mid
        if gross + fee_twd(gross) * 100 <= cash_cents:
            lo = mid
        else:
            hi = mid - 1
    return lo


def apply_decisions(path, run_id, decisions, candidates):
    """Fill validated BUY/SELL suggestions at LIVE last prices within cash/position limits."""
    by_symbol = {c['symbol']: c for c in candidates}
    fills = []
    db = connect(path)
    try:
        with db:
            account_row = db.execute('SELECT * FROM paper_account WHERE account_id=1').fetchone()
            if account_row is None:
                raise ValueError('PAPER_CAPITAL_NOT_CONFIGURED')
            cash = account_row['cash_cents']
            for decision in decisions:
                side = decision.get('decision')
                symbol = decision.get('symbol')
                candidate = by_symbol.get(symbol, {})
                if side not in ('BUY', 'SELL') or candidate.get('quote_status') != 'LIVE' or candidate.get('is_trial'):
                    continue
                price = candidate.get('last_price')
                if price is None:
                    continue
                cents = price_cents(price)
                pos = db.execute('SELECT * FROM paper_positions WHERE symbol=?', (symbol,)).fetchone()
                held = pos['qty'] if pos else 0
                proposed = decision.get('suggested_qty')
                if type(proposed) is not int or proposed <= 0:
                    continue
                if side == 'BUY':
                    qty = min(proposed, max_affordable_qty(cash, price))
                    if qty <= 0:
                        continue
                    gross = cents * qty
                    fee = fee_twd(gross)
                    total_cost = gross + fee * 100
                    if total_cost > cash:
                        raise RuntimeError('PAPER_CASH_GUARD_REJECTED_BUY')
                    old_cost = pos['cost_cents'] if pos else 0
                    new_qty, new_cost = held + qty, old_cost + total_cost
                    cash -= total_cost
                    cost_basis, realized = total_cost, 0
                else:
                    qty = min(proposed, held, MAX_ODDLOT_QTY)
                    if qty <= 0:
                        continue
                    gross = cents * qty
                    fee = fee_twd(gross)
                    cost_basis = pos['cost_cents'] if qty == held else (pos['cost_cents'] * qty // held)
                    new_qty, new_cost = held - qty, pos['cost_cents'] - cost_basis
                    net_proceeds = gross - fee * 100
                    cash += net_proceeds
                    realized = net_proceeds - cost_basis
                at = now().isoformat()
                if new_qty:
                    db.execute('INSERT OR REPLACE INTO paper_positions VALUES(?,?,?,?)',
                               (symbol, new_qty, new_cost, at))
                else:
                    db.execute('DELETE FROM paper_positions WHERE symbol=?', (symbol,))
                cash_change = -total_cost if side == 'BUY' else gross - fee * 100
                db.execute('''INSERT INTO paper_trades(
                    run_id,occurred_at,symbol,name,side,qty,price_cents,gross_cents,fee_twd,
                    cash_change_cents,cost_basis_cents,realized_pnl_cents,reason)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (run_id, at, symbol, candidate.get('name') or '股票名稱未取得', side, qty, cents, gross, fee, cash_change,
                     cost_basis, realized, decision.get('reason', '')))
                fills.append(dict(symbol=symbol, side=side, qty=qty, price_twd=cents/100,
                                  gross_twd=gross/100, fee_twd=fee,
                                  cash_change_twd=cash_change/100,
                                  cost_basis_twd=cost_basis/100,
                                  realized_pnl_twd=realized/100))
            db.execute('UPDATE paper_account SET cash_cents=?,updated_at=? WHERE account_id=1',
                       (cash, now().isoformat()))
        current = account(path)
        return dict(account=current, available_cash_twd=current['cash_cents']/100,
                    capital_limit_twd=current['capital_limit_cents']/100,
                    positions=positions(path), trades=fills)
    finally:
        db.close()


def snapshot(path):
    acct = account(path)
    if acct is None:
        return None
    return dict(capital_limit_twd=acct['capital_limit_cents']/100,
                available_cash_cents=acct['cash_cents'],
                available_cash_twd=acct['cash_cents']/100,
                positions=positions(path))
