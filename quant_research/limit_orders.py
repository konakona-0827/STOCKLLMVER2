"""Durable planned-price PAPER orders; no broker/network imports.

Price plans, accepted orders and confirmed simulated fills are distinct records.
Existing live risk configurations reject PLANNED_LIMIT by default. A quote
touch alone cannot fill: a strictly later executable odd-lot book is required.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from contextlib import closing
import json
import re
import sqlite3

from execution.models import AdviceIntent, Side
from execution.risk_guard import MarketQuote, PositionState, RiskConfig, build_order_plan
from market_calendar import twse_closed_reason
from .core import Policy, Costs
from .forward import ForwardPaper
from .runner import RiskEngine

VERSION = 'planned-limit-paper-v1'
TAIPEI = timezone(timedelta(hours=8))
ACTIVE = ('ACKNOWLEDGED', 'PARTIALLY_FILLED', 'CANCEL_PENDING', 'UNCONFIRMED')
TERMINAL = ('FILLED', 'CANCELLED', 'EXPIRED')


def aware(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return stamp.astimezone(TAIPEI)


def positive(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError('POSITIVE_FINITE_REQUIRED')
    return number


def stock_tick(price):
    price = positive(price)
    for ceiling, step in ((10, '.01'), (50, '.05'), (100, '.1'), (500, '.5'), (1000, '1')):
        if price < ceiling:
            return Decimal(step)
    return Decimal(5)


def floor_stock_price(price):
    price = positive(price)
    step = stock_tick(price)
    return (price/step).to_integral_value(rounding=ROUND_FLOOR)*step


@dataclass(frozen=True)
class PricePlan:
    plan_id: str
    symbol: str
    side: str
    qty: int
    limit_price: str
    target_price: str
    stop_price: str
    created_at: str
    expires_at: str
    rationale: str
    strategy_version: str
    model_version: str
    invalidation_rule: str = 'TREND_OR_EVENT_INVALIDATED'
    sector: str = 'UNKNOWN'

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in
                   (self.plan_id, self.rationale, self.strategy_version, self.model_version, self.invalidation_rule)):
            raise ValueError('PLAN_PROVENANCE_REQUIRED')
        if not re.fullmatch(r'[1-9][0-9]{3}', self.symbol):
            raise ValueError('ORDINARY_STOCKS_ONLY')
        if self.side not in ('BUY', 'SELL') or type(self.qty) is not int or not 1 <= self.qty <= 999:
            raise ValueError('INVALID_ODDLOT_ORDER')
        price, target, stop = [positive(v) for v in (self.limit_price, self.target_price, self.stop_price)]
        if price % stock_tick(price) != 0:
            raise ValueError('INVALID_STOCK_TICK')
        if self.side == 'BUY' and not stop < price < target:
            raise ValueError('INVALID_BUY_BRACKET')
        created, expires = aware(self.created_at), aware(self.expires_at)
        if created >= expires or twse_closed_reason(expires.date()) or expires.strftime('%H:%M:%S') > '13:30:00':
            raise ValueError('INVALID_ROD_EXPIRY')


class LimitOrderBook:
    """One independent strategy account; uses existing Paper state/cost schema."""
    def __init__(self, path, policy: Policy, costs=Costs(), initial=100000):
        positive(initial)
        existing_path = Path(path).resolve()
        if existing_path.exists():
            with closing(sqlite3.connect(existing_path.as_uri()+'?mode=ro', uri=True)) as db:
                names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if names & {'execution_attempts', 'ai_positions', 'scan_runs'}:
                    raise ValueError('LIVE_OR_ANALYSIS_LEDGER_FORBIDDEN')
        self.paper = ForwardPaper(path, policy, costs, initial)
        self.policy, self.costs = policy, costs
        with self.paper.connect() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'plan_meta' not in tables and (db.execute('SELECT count(*) FROM events').fetchone()[0]
                    or db.execute('SELECT count(*) FROM positions').fetchone()[0]):
                raise ValueError('NEW_PLANNED_EXPERIMENT_REQUIRED')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS plan_meta(version TEXT);
                CREATE TABLE IF NOT EXISTS price_plans(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS planned_orders(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS planned_fills(id TEXT PRIMARY KEY, order_id TEXT, payload TEXT);
                CREATE TABLE IF NOT EXISTS plan_events(id INTEGER PRIMARY KEY, at TEXT, plan_id TEXT, kind TEXT, payload TEXT);
                CREATE TABLE IF NOT EXISTS book_ticks(id TEXT PRIMARY KEY, at TEXT, payload TEXT);
                CREATE TABLE IF NOT EXISTS book_inputs(id TEXT PRIMARY KEY, payload TEXT);
            ''')
            row = db.execute('SELECT version FROM plan_meta').fetchone()
            if row and row[0] != VERSION:
                raise ValueError('NEW_PLANNED_VERSION_REQUIRES_NEW_LEDGER')
            if row is None:
                db.execute('INSERT INTO plan_meta VALUES(?)', (VERSION,))

    @staticmethod
    def _event(db, at, pid, kind, payload):
        db.execute('INSERT INTO plan_events(at,plan_id,kind,payload) VALUES(?,?,?,?)',
                   (at, pid, kind, json.dumps(payload)))

    def create(self, plan: PricePlan):
        encoded = json.dumps(asdict(plan), sort_keys=True)
        with self.paper.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT payload FROM price_plans WHERE id=?', (plan.plan_id,)).fetchone()
            if previous:
                if previous[0] != encoded:
                    raise ValueError('PLAN_ID_IMMUTABLE')
                return
            db.execute('INSERT INTO price_plans VALUES(?,?)', (plan.plan_id, encoded))
            self._event(db, plan.created_at, plan.plan_id, 'PLAN_CREATED', asdict(plan))

    @staticmethod
    def _orders(db):
        return {key: json.loads(raw) for key, raw in db.execute('SELECT id,payload FROM planned_orders')}

    @staticmethod
    def _time_guard(db, at):
        times = [aware(r[0]) for r in db.execute('SELECT at FROM plan_events UNION ALL SELECT at FROM book_ticks')]
        if times and aware(at) < max(times):
            raise ValueError('OUT_OF_ORDER_PLAN_EVENT')

    def _reserve(self, order):
        if order['status'] not in ACTIVE or order['side'] != 'BUY':
            return 0
        remaining = order['qty']-order['filled_qty']
        gross = float(order['limit_price'])*remaining
        return gross + max(0, self.costs.fee(order['gross']+gross)-order['fee_paid'])

    @staticmethod
    def _quote(symbol, quotes, at):
        q = quotes[symbol]
        age = (aware(at)-aware(q['quote_timestamp'])).total_seconds()
        if q.get('symbol') != symbol or q.get('quote_status') != 'LIVE' or q.get('is_trial') or not 0 <= age <= 120:
            raise ValueError('STALE_OR_INVALID_QUOTE')
        if q.get('lot_type') != 'INTRADAY_ODD_LOT' or q.get('book_size_unit') != 'shares':
            raise ValueError('VERIFIED_ODDLOT_BOOK_REQUIRED')
        bid, ask = positive(q['bid']), positive(q['ask'])
        if bid > ask:
            raise ValueError('CROSSED_BOOK')
        for key in ('bid_size', 'ask_size'):
            if type(q.get(key)) is not int or q[key] < 0:
                raise ValueError('INVALID_BOOK_SIZE')
        return q, MarketQuote(symbol, 'LIVE', Decimal(str(age)), positive(q['last_price']), bid, ask)

    def submit(self, plan_id, at, quotes, *, invalidated=False):
        stamp = aware(at)
        with self.paper.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            orders = self._orders(db)
            if plan_id in orders:
                return orders[plan_id]  # restart/retry never generates a second order
            self._time_guard(db, at)
            row = db.execute('SELECT payload FROM price_plans WHERE id=?', (plan_id,)).fetchone()
            if not row:
                raise ValueError('UNKNOWN_PLAN')
            p = PricePlan(**json.loads(row[0]))
            if invalidated or stamp < aware(p.created_at) or stamp >= aware(p.expires_at):
                raise ValueError('PLAN_EXPIRED_OR_INVALIDATED')
            if stamp.date() != aware(p.expires_at).date() or not '09:00:00' <= stamp.strftime('%H:%M:%S') < '13:30:00':
                raise ValueError('OUTSIDE_PLAN_ROD_SESSION')
            if any(o['symbol'] == p.symbol and o['status'] in ACTIVE for o in orders.values()):
                raise ValueError('SYMBOL_HAS_PENDING_ORDER')
            q, quote = self._quote(p.symbol, quotes, at)
            cash, peak, _ = db.execute('SELECT cash,peak,config FROM state').fetchone()
            holdings = {s: json.loads(raw) for s, raw in db.execute('SELECT symbol,payload FROM positions')}
            equity = cash + db.execute('SELECT COALESCE(sum(amount),0) FROM settlement').fetchone()[0]
            for symbol, held in holdings.items():
                _, mark = self._quote(symbol, quotes, at)
                equity += self.costs.sell(float(mark.bid), held['qty'])
            free = cash-sum(self._reserve(o) for o in orders.values())
            held = holdings.get(p.symbol, {})
            if p.side == 'BUY':
                pending = dict(holdings)
                for o in orders.values():
                    if o['side'] == 'BUY' and o['status'] in ACTIVE and o['symbol'] not in pending:
                        pending[o['symbol']] = dict(cost=self._reserve(o), sector=o['sector'])
                block = RiskEngine().check(p.symbol, p.sector, p.qty, float(p.limit_price), free,
                    equity, max(peak, equity), pending, self.policy, self.costs)
                if block:
                    raise ValueError(block)
                # Assess the chosen entry price, not today's ask. Spread/slippage
                # is still reserved in the expected exit, never waived.
                entry, target, stop = map(float, (p.limit_price, p.target_price, p.stop_price))
                if self.costs.pnl(entry, target, p.qty) <= 0:
                    raise ValueError('TARGET_CANNOT_COVER_COSTS')
                if -self.costs.pnl(entry, stop, p.qty) > equity*self.policy.risk_fraction:
                    raise ValueError('PLAN_STOP_EXCEEDS_LOSS_BUDGET')
            elif (held.get('qty', 0) < p.qty or aware(held['at']).date() >= stamp.date()):
                raise ValueError('SELL_QTY_OR_SAME_DAY')
            intent = AdviceIntent(p.plan_id, p.plan_id, '1.0', p.symbol, Side(p.side), Decimal(0),
                p.rationale, 'PRICE_PLAN', (), 'LIVE', q['quote_timestamp'], quote.age_seconds,
                quote.last_price, positive(p.limit_price), p.qty, Decimal(0), held.get('qty', 0),
                pricing_policy='PLANNED_LIMIT')
            order_plan = build_order_plan(intent, quote=quote,
                position=PositionState(p.symbol, ai_managed_qty=held.get('qty', 0)),
                config=RiskConfig(max_buy_qty=999, allow_planned_limits=True))
            order = asdict(p)
            order.update(status='ACKNOWLEDGED', submitted_at=at,
                submitted_quote_at=q['quote_timestamp'], filled_qty=0, gross=0., fee_paid=0., tax_paid=0.,
                limit_price=str(order_plan.limit_price), pricing_policy='PLANNED_LIMIT',
                transport='PAPER_ONLY', version=VERSION)
            db.execute('INSERT INTO planned_orders VALUES(?,?)', (plan_id, json.dumps(order)))
            db.execute('UPDATE state SET peak=? WHERE id=1', (max(peak, equity),))
            self._event(db, at, plan_id, 'ACKNOWLEDGED', order)
            return order

    def request_cancel(self, plan_id, at, reason):
        with self.paper.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            orders = self._orders(db)
            o = orders[plan_id]
            if aware(at) < aware(o['submitted_at']):
                raise ValueError('OUT_OF_ORDER_CANCEL')
            if o['status'] in TERMINAL:
                return o
            self._time_guard(db, at)
            o.update(status='CANCEL_PENDING', cancel_reason=reason, cancel_requested_at=at)
            db.execute('UPDATE planned_orders SET payload=? WHERE id=?', (json.dumps(o), plan_id))
            self._event(db, at, plan_id, 'CANCEL_REQUESTED', dict(reason=reason))
            return o

    def confirm_cancel(self, plan_id, at):
        """Simulator acknowledgement. Cancellation requests never release cash."""
        with self.paper.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            o = self._orders(db)[plan_id]
            if o['status'] in TERMINAL:
                return o
            self._time_guard(db, at)
            if o['status'] != 'CANCEL_PENDING' or aware(at) < aware(o['cancel_requested_at']):
                raise ValueError('CANCEL_NOT_REQUESTED')
            o.update(status='CANCELLED', cancelled_at=at)
            db.execute('UPDATE planned_orders SET payload=? WHERE id=?', (json.dumps(o), plan_id))
            self._event(db, at, plan_id, 'CANCEL_CONFIRMED', o)
            return o

    def snapshot(self):
        with self.paper.connect() as db:
            orders = self._orders(db)
            cash = db.execute('SELECT cash FROM state').fetchone()[0]
            reserved = sum(self._reserve(o) for o in orders.values())
            return dict(version=VERSION, live_enabled=False, cash=cash, reserved=reserved,
                available=cash-reserved, orders=list(orders.values()),
                reserved_sell_qty={o['symbol']: o['qty']-o['filled_qty'] for o in orders.values()
                                   if o['side'] == 'SELL' and o['status'] in ACTIVE},
                positions={s: json.loads(raw) for s, raw in db.execute('SELECT symbol,payload FROM positions')},
                fills=[json.loads(r[0]) for r in db.execute('SELECT payload FROM planned_fills')])

    def process(self, tick_id, at, quotes, *, sessions=(), invalidated_symbols=()):
        """Deterministic book proxy, explicit available depth, no high/low fills.

        A tick ID is processed once and each displayed share is consumed at most
        once per book snapshot across all orders. No same-snapshot fill on submit.
        """
        stamp = aware(at)
        encoded_input = json.dumps(dict(at=at, quotes=quotes, sessions=list(sessions),
                                       invalidated_symbols=sorted(invalidated_symbols)), sort_keys=True)
        with self.paper.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT payload FROM book_ticks WHERE id=?', (tick_id,)).fetchone()
            if old:
                old_input = db.execute('SELECT payload FROM book_inputs WHERE id=?', (tick_id,)).fetchone()
                if old_input and old_input[0] != encoded_input:
                    raise ValueError('TICK_ID_INPUT_MISMATCH')
                return json.loads(old[0])
            self._time_guard(db, at)
            previous = [aware(r[0]) for r in db.execute('SELECT at FROM book_ticks')]
            if previous and stamp <= max(previous):
                raise ValueError('OUT_OF_ORDER_BOOK_TICK')
            cash = db.execute('SELECT cash FROM state').fetchone()[0]
            due = db.execute('SELECT COALESCE(sum(amount),0) FROM settlement WHERE due<=?', (stamp.date().isoformat(),)).fetchone()[0]
            cash += due
            db.execute('DELETE FROM settlement WHERE due<=?', (stamp.date().isoformat(),))
            orders = self._orders(db)
            results, consumed = [], {}
            for pid, o in sorted(orders.items(), key=lambda item: (aware(item[1]['submitted_at']), item[0])):
                if o['status'] not in ACTIVE:
                    continue
                if stamp >= aware(o['expires_at']):
                    # Paper-only ROD expiry. Never infer broker cancellation.
                    o.update(status='EXPIRED', expired_at=at)
                    self._event(db, at, pid, 'PAPER_ROD_EXPIRED', o)
                elif o['symbol'] in invalidated_symbols:
                    o.update(status='CANCEL_PENDING', cancel_requested_at=at, cancel_reason='PLAN_INVALIDATED')
                    self._event(db, at, pid, 'CANCEL_REQUESTED', o)
                elif o['status'] != 'UNCONFIRMED':
                    try:
                        q, quote = self._quote(o['symbol'], quotes, at)
                        if (aware(q['quote_timestamp']) <= max(aware(o['submitted_quote_at']), aware(o['submitted_at']))
                                or twse_closed_reason(stamp.date())
                                or not '09:10:00' <= stamp.strftime('%H:%M:%S') <= '13:30:00'):
                            raise ValueError('WAIT_NEW_EXECUTABLE_BOOK')
                        price = float(quote.ask if o['side'] == 'BUY' else quote.bid)
                        limit = float(o['limit_price'])
                        executable = price <= limit if o['side'] == 'BUY' else price >= limit
                        if not executable:
                            raise ValueError('WAIT_LIMIT_PRICE')
                        key = (o['symbol'], o['side'], q['quote_timestamp'])
                        size = q['ask_size' if o['side'] == 'BUY' else 'bid_size']
                        already = sum(f['qty'] for f in
                            [json.loads(r[0]) for r in db.execute('SELECT payload FROM planned_fills WHERE order_id=?', (pid,))]
                            if f['quote_at'] == q['quote_timestamp'])
                        qty = min(o['qty']-o['filled_qty'], max(0, size-max(already, consumed.get(key, 0))))
                        if qty == 0:
                            raise ValueError('WAIT_LIQUIDITY')
                        gross = price*qty
                        fee = self.costs.fee(o['gross']+gross)-o['fee_paid']
                        row = db.execute('SELECT payload FROM positions WHERE symbol=?', (o['symbol'],)).fetchone()
                        held = json.loads(row[0]) if row else None
                        tax = 0
                        fill = dict(id=tick_id+':'+pid, order_id=pid, at=at, quote_at=q['quote_timestamp'],
                                    side=o['side'], symbol=o['symbol'], qty=qty, price=price, fee=fee)
                        if o['side'] == 'BUY':
                            # Execution never exceeds limit; no synthetic price slippage.
                            # Fees remain separate from the price cap.
                            cost = gross+fee
                            if cost > cash:
                                raise ValueError('CASH_RECONCILIATION_REQUIRED')
                            cash -= cost
                            old_qty = held['qty'] if held else 0
                            old_cost = held['cost'] if held else 0
                            held = dict(qty=old_qty+qty, cost=old_cost+cost, sector=o['sector'],
                                        at=held['at'] if held else at,
                                        entry=(old_cost+gross+fee)/(old_qty+qty),
                                        target=float(o['target_price']), stop=float(o['stop_price']),
                                        next_review='NEXT_SESSION_OR_EVENT')
                        else:
                            if not held or held['qty'] < qty or aware(held['at']).date() >= stamp.date():
                                raise ValueError('SELL_QTY_OR_SAME_DAY')
                            future = sorted(set(s for s in sessions if s > stamp.date().isoformat()))
                            if len(future) < 2:
                                raise ValueError('VERIFIED_T_PLUS_2_REQUIRED')
                            from math import ceil
                            tax = ceil((o['gross']+gross)*self.costs.tax)-o['tax_paid']
                            proceeds = gross-fee-tax
                            basis = held['cost']*qty/held['qty']
                            fill.update(realized_net=proceeds-basis)
                            held.update(qty=held['qty']-qty, cost=held['cost']-basis)
                            db.execute('INSERT INTO settlement VALUES(?,?)', (future[1], proceeds))
                        if held['qty']:
                            db.execute('INSERT OR REPLACE INTO positions VALUES(?,?)', (o['symbol'], json.dumps(held)))
                        else:
                            db.execute('DELETE FROM positions WHERE symbol=?', (o['symbol'],))
                        consumed[key] = consumed.get(key, 0)+qty
                        fill['tax'] = tax
                        db.execute('INSERT INTO planned_fills VALUES(?,?,?)', (fill['id'], pid, json.dumps(fill)))
                        o.update(filled_qty=o['filled_qty']+qty, gross=o['gross']+gross,
                                 fee_paid=o['fee_paid']+fee, tax_paid=o['tax_paid']+tax)
                        if o['filled_qty'] == o['qty']:
                            o['status'] = 'FILLED'
                        elif o['status'] != 'CANCEL_PENDING':
                            o['status'] = 'PARTIALLY_FILLED'
                        results.append(fill)
                        self._event(db, at, pid, 'CONFIRMED_PAPER_FILL', fill)
                    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
                        self._event(db, at, pid, 'WAIT', dict(reason=str(exc)))
                db.execute('UPDATE planned_orders SET payload=? WHERE id=?', (json.dumps(o), pid))
            db.execute('UPDATE state SET cash=? WHERE id=1', (cash,))
            db.execute('INSERT INTO book_ticks VALUES(?,?,?)', (tick_id, at, json.dumps(results)))
            db.execute('INSERT INTO book_inputs VALUES(?,?)', (tick_id, encoded_input))
            return results
