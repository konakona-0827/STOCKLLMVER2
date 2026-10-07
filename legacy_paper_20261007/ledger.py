"""Single SQLite paper ledger. Amounts are stored in integer cents."""
import json
import sqlite3
import uuid
from decimal import Decimal, ROUND_HALF_UP
from config import MAX_STRATEGY_CAPITAL_TWD, now, redact


def cents(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def fee(value):
    return max(2000, int((Decimal(value) * Decimal('.001425')).quantize(Decimal('1'), rounding=ROUND_HALF_UP)))


def dumps(value):
    return redact(json.dumps(value, ensure_ascii=False, allow_nan=False))


class Ledger:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS strategy_cycles (
            cycle_id TEXT PRIMARY KEY, timestamp TEXT, status TEXT, raw_llm_response TEXT,
            input_json TEXT, api_json TEXT, summary TEXT);
          CREATE TABLE IF NOT EXISTS decisions (
            decision_id INTEGER PRIMARY KEY, cycle_id TEXT REFERENCES strategy_cycles(cycle_id),
            timestamp TEXT, symbol TEXT, action TEXT, amount REAL, qty INTEGER, reason TEXT,
            raw_llm_response TEXT, llm_wants_order INTEGER, simulated_order_sent INTEGER DEFAULT 0,
            outcome TEXT, source TEXT);
          CREATE TABLE IF NOT EXISTS orders (
            local_order_id TEXT PRIMARY KEY, decision_id INTEGER UNIQUE REFERENCES decisions(decision_id),
            symbol TEXT, side TEXT, qty INTEGER, price REAL, estimated_value INTEGER,
            status TEXT, broker_seq TEXT, submitted_at TEXT, updated_at TEXT,
            execution_mode TEXT NOT NULL DEFAULT 'PAPER');
          CREATE TABLE IF NOT EXISTS fills (
            fill_id INTEGER PRIMARY KEY, local_order_id TEXT UNIQUE REFERENCES orders(local_order_id),
            timestamp TEXT, symbol TEXT, side TEXT, qty INTEGER, price REAL,
            value_cents INTEGER, fee_cents INTEGER, tax_cents INTEGER,
            execution_mode TEXT NOT NULL DEFAULT 'PAPER');
          CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY, timestamp TEXT, cycle_id TEXT, code TEXT, detail TEXT);
          CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, timestamp TEXT, kind TEXT, detail TEXT);
          CREATE TABLE IF NOT EXISTS quotes (
            symbol TEXT, exchange_time TEXT, fetched_at TEXT, payload TEXT,
            PRIMARY KEY(symbol, exchange_time));
        ''')

    def event(self, kind, detail):
        with self.db:
            self.db.execute('INSERT INTO events(timestamp,kind,detail) VALUES(?,?,?)',
                            (now().isoformat(), kind, dumps(detail)))

    def error(self, code, detail, cycle=None):
        with self.db:
            self.db.execute('INSERT INTO errors(timestamp,cycle_id,code,detail) VALUES(?,?,?,?)',
                            (now().isoformat(), cycle, code, redact(detail)))

    def claim_cycle(self, cycle):
        with self.db:
            cur = self.db.execute('INSERT OR IGNORE INTO strategy_cycles(cycle_id,timestamp,status) VALUES(?,?,?)',
                                  (cycle, now().isoformat(), 'STARTED'))
        return cur.rowcount == 1

    def save_cycle(self, cycle, status, payload=None, raw='', api=None, summary=''):
        with self.db:
            self.db.execute('''UPDATE strategy_cycles SET status=?,input_json=?,raw_llm_response=?,
                              api_json=?,summary=? WHERE cycle_id=?''',
                            (status, dumps(payload or {}), redact(raw), dumps(api or {}), redact(summary), cycle))

    def store_quotes(self, quotes):
        with self.db:
            self.db.executemany('''INSERT INTO quotes VALUES(?,?,?,?)
                ON CONFLICT(symbol,exchange_time) DO UPDATE SET fetched_at=excluded.fetched_at,payload=excluded.payload
                WHERE json_extract(excluded.payload,'$.source')='CAPITAL_SKCOM' ''',
                                [(q['symbol'], q['exchange_time'], q['fetched_at'], dumps(q)) for q in quotes])

    def history(self, symbol, at, source=None):
        rows = self.db.execute('''SELECT payload FROM quotes WHERE symbol=? AND exchange_time>=?
                                 AND exchange_time<=? AND (? IS NULL OR json_extract(payload,'$.source')=?)
                                 ORDER BY exchange_time DESC LIMIT 60''',
                               (symbol, at.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
                                at.isoformat(), source, source)).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def latest(self, symbol, source=None):
        row = self.db.execute('''SELECT payload FROM quotes WHERE symbol=?
            AND (? IS NULL OR json_extract(payload,'$.source')=?) ORDER BY exchange_time DESC LIMIT 1''',
                              (symbol, source, source)).fetchone()
        return json.loads(row[0]) if row else None

    def portfolio(self):
        positions, cash = {}, MAX_STRATEGY_CAPITAL_TWD * 100
        for r in self.db.execute('SELECT * FROM fills ORDER BY fill_id'):
            p = positions.setdefault(r['symbol'], {'qty': 0, 'cost_cents': 0})
            if r['side'] == 'BUY':
                cost = r['value_cents'] + r['fee_cents']
                p['qty'] += r['qty']
                p['cost_cents'] += cost
                cash -= cost
            else:
                removed = p['cost_cents'] if r['qty'] == p['qty'] else p['cost_cents'] * r['qty'] // p['qty']
                p['qty'] -= r['qty']
                p['cost_cents'] -= removed
                cash += r['value_cents'] - r['fee_cents'] - r['tax_cents']
        positions = {s: p for s, p in positions.items() if p['qty']}
        reserved = self.db.execute('''SELECT COALESCE(SUM(estimated_value),0) FROM orders
            WHERE side='BUY' AND status IN ('CREATED','SUBMITTED','ACKNOWLEDGED','PARTIAL','UNKNOWN')''').fetchone()[0]
        used = sum(p['cost_cents'] for p in positions.values())
        return dict(initial_cash_twd=MAX_STRATEGY_CAPITAL_TWD, cash_twd=cash / 100,
                    used_capital=used / 100, reserved_capital=reserved / 100,
                    committed=(used + reserved) / 100, positions=positions)

    def decision(self, cycle, item, raw, source='LLM'):
        with self.db:
            cur = self.db.execute('''INSERT INTO decisions(cycle_id,timestamp,symbol,action,amount,qty,reason,
                 raw_llm_response,llm_wants_order,outcome,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (cycle, now().isoformat(), item.get('symbol', ''), item['action'], 0,
                 item.get('qty', 0), redact(item['reason']), redact(raw),
                 int(source == 'LLM' and item['action'] in {'BUY', 'SELL'}), 'NO_ORDER', source))
        return cur.lastrowid

    def outcome(self, decision_id, outcome):
        with self.db:
            self.db.execute('UPDATE decisions SET outcome=? WHERE decision_id=?', (outcome, decision_id))

    def paper_fill(self, decision_id, symbol, side, qty, price):
        """Atomically reserve, risk-check and fill an idealized paper order; no network."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.db.execute('SELECT 1 FROM orders WHERE decision_id=?', (decision_id,)).fetchone():
                return 'ALREADY_SIMULATED', None
            if side not in {'BUY', 'SELL'} or type(qty) is not int or qty <= 0 or price <= 0:
                return 'INVALID_ORDER', None
            p = self.portfolio()
            value = cents(price) * qty
            commission = fee(value)
            estimated = value + commission
            owned = p['positions'].get(symbol, {}).get('qty', 0)
            if side == 'BUY' and cents(p['committed']) + estimated > MAX_STRATEGY_CAPITAL_TWD * 100:
                return 'CAPITAL_LIMIT_REJECTED', None
            if side == 'BUY' and estimated > cents(p['cash_twd'] - p['reserved_capital']):
                return 'CASH_LIMIT_REJECTED', None
            if side == 'SELL' and qty > owned:
                return 'SELL_REJECTED_LOCAL_OWNERSHIP', None
            tax = int((Decimal(value) * Decimal('.003')).quantize(Decimal('1'), rounding=ROUND_HALF_UP)) if side == 'SELL' else 0
            order_id, stamp = 'PAPER-' + uuid.uuid4().hex, now().isoformat()
            self.db.execute('''INSERT INTO orders(local_order_id,decision_id,symbol,side,qty,price,
               estimated_value,status,submitted_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
               (order_id, decision_id, symbol, side, qty, price, estimated, 'FILLED', stamp, stamp))
            self.db.execute('''INSERT INTO fills(local_order_id,timestamp,symbol,side,qty,price,
               value_cents,fee_cents,tax_cents) VALUES(?,?,?,?,?,?,?,?,?)''',
               (order_id, stamp, symbol, side, qty, price, value, commission, tax))
            self.db.execute('''UPDATE decisions SET simulated_order_sent=1,outcome='PAPER_FILLED',amount=?
                               WHERE decision_id=?''', (value / 100, decision_id))
        return 'PAPER_FILLED', order_id

    def close(self):
        self.db.close()
