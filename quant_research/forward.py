"""Forward paper adapter using the existing AdviceIntent / Risk Guard contract.

Explicit bid/ask size required. No broker transport imports or automatic retry.
SQLite transaction covers idempotency, funds, positions and the audit event.
"""
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from math import isfinite
import json
import sqlite3

from execution.models import AdviceIntent, Side
from execution.risk_guard import build_order_plan, MarketQuote, PositionState, RiskConfig
from market_calendar import twse_closed_reason
from .core import Costs, identity
from .runner import RiskEngine


class ForwardPaper:
    def __init__(self, path, policy, costs=Costs(), initial=100000):
        self.path, self.policy, self.costs = Path(path), policy, costs
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY, cash REAL, peak REAL, config TEXT);
                CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY, payload TEXT);
                CREATE TABLE IF NOT EXISTS events(decision_id TEXT PRIMARY KEY, at TEXT, payload TEXT);
                CREATE TABLE IF NOT EXISTS settlement(due TEXT, amount REAL);
            ''')
            config = json.dumps(identity(policy, costs), sort_keys=True)
            db.execute('INSERT OR IGNORE INTO state VALUES(1,?,?,?)', (initial, initial, config))
            if db.execute('SELECT config FROM state').fetchone()[0] != config:
                raise ValueError('NEW_VERSION_REQUIRES_NEW_LEDGER')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def execute(self, decision_id, at, symbol, side, qty, quote, *, sector='UNKNOWN',
                settlement_date=None, reason='', sessions=(), portfolio_marks=None):
        """One immutable intent; rejected or unfilled intents require a new ID.

        settlement_date must come from a verified market calendar maintained by
        the caller; dates are checked against the supplied exchange sessions.
        """
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT payload FROM events WHERE decision_id=?', (decision_id,)).fetchone()
            if prior:
                return json.loads(prior[0])
            prior_times = [datetime.fromisoformat(r[0]) for r in db.execute('SELECT at FROM events')]
            incoming = datetime.fromisoformat(at)
            if incoming.tzinfo is None:
                raise ValueError('TIMEZONE_REQUIRED')
            if prior_times and incoming < max(prior_times):
                raise ValueError('OUT_OF_ORDER_CYCLE')
            stamp = datetime.fromisoformat(at).astimezone(timezone(timedelta(hours=8)))
            day = stamp.date().isoformat()
            cash, peak, _ = db.execute('SELECT cash,peak,config FROM state').fetchone()
            cash += db.execute('SELECT COALESCE(sum(amount),0) FROM settlement WHERE due<=?', (day,)).fetchone()[0]
            db.execute('DELETE FROM settlement WHERE due<=?', (day,))
            positions = {s: json.loads(p) for s, p in db.execute('SELECT symbol,payload FROM positions')}
            result = dict(decision_id=decision_id, at=at, symbol=symbol, side=side,
                          requested_qty=qty, filled_qty=0, status='REJECTED', reason=reason,
                          identity=identity(self.policy, self.costs), ledger='FORWARD_PAPER')
            try:
                if datetime.fromisoformat(at).tzinfo is None:
                    raise ValueError('TIMEZONE_REQUIRED')
                if twse_closed_reason(stamp.date()) or not ('09:10' <= stamp.strftime('%H:%M') <= '13:30'):
                    raise ValueError('OUTSIDE_ODDLOT_SESSION')
                if type(qty) is not int or not 1 <= qty <= 999 or side not in ('BUY', 'SELL'):
                    raise ValueError('INVALID_ORDER')
                if quote.get('is_trial') or quote.get('symbol') != symbol:
                    raise ValueError('INVALID_QUOTE')
                qt = datetime.fromisoformat(quote['quote_timestamp'])
                if qt.tzinfo is None:
                    raise ValueError('TIMEZONE_REQUIRED')
                age = (stamp-qt).total_seconds()
                if not 0 <= age <= 120:
                    raise ValueError('STALE_OR_FUTURE_QUOTE')
                bid, ask = float(quote['bid']), float(quote['ask'])
                if not all(isfinite(v) for v in (bid, ask)) or not 0 < bid <= ask or (ask-bid)/ask > .005:
                    raise ValueError('SPREAD_TOO_WIDE')
                if not quote.get('lot_type') == 'INTRADAY_ODD_LOT':
                    raise ValueError('WRONG_BOOK')
                if quote.get('book_size_unit') != 'shares':
                    raise ValueError('BOOK_SIZE_UNIT_UNKNOWN')
                available = quote.get('ask_size' if side == 'BUY' else 'bid_size')
                if type(available) is not int or available < 0:
                    raise ValueError('BOOK_SIZE_UNKNOWN')
                fill = min(qty, available)
                if fill == 0:
                    raise ValueError('UNFILLED_NO_LIQUIDITY')
                held = positions.get(symbol)
                marks = portfolio_marks or {}
                if side == 'BUY' and any(s not in marks or not isfinite(marks[s]) or marks[s] <= 0 for s in positions):
                    raise ValueError('PORTFOLIO_MARKS_REQUIRED')
                equity = cash + db.execute('SELECT COALESCE(sum(amount),0) FROM settlement').fetchone()[0]
                equity += sum(self.costs.sell(marks.get(s, p['entry']), p['qty']) for s, p in positions.items())
                peak = max(peak, equity)
                if side == 'BUY':
                    block = RiskEngine().check(symbol, sector, qty, ask, cash, equity, peak,
                                               positions, self.policy, self.costs)
                    if block:
                        raise ValueError(block)
                elif not held or qty > held['qty'] or held['at'][:10] >= day:
                    raise ValueError('SELL_QTY_OR_SAME_DAY')
                if side == 'SELL':
                    future_sessions = sorted(s for s in sessions if s > day)
                    if len(future_sessions) < 2 or settlement_date != future_sessions[1]:
                        raise ValueError('VERIFIED_T_PLUS_2_REQUIRED')
                intent = AdviceIntent(decision_id, decision_id, '1.0', symbol, Side(side),
                    Decimal(0), reason, 'QUANT', (), quote['quote_status'], quote['quote_timestamp'],
                    Decimal(str(age)), Decimal(str(quote['last_price'])), None, qty, Decimal(0),
                    held['qty'] if held else 0)
                market = MarketQuote.from_mapping(symbol, dict(quote, age_seconds=age))
                build_order_plan(intent, quote=market,
                                 position=PositionState(symbol, ai_managed_qty=held['qty'] if held else 0),
                                 config=RiskConfig(max_buy_qty=999))
                price = ask if side == 'BUY' else bid
                if side == 'BUY':
                    cost = self.costs.buy(price, fill)
                    cash -= cost
                    payload = dict(qty=fill, cost=cost, sector=sector, at=at, entry=price,
                                   target=price*(1+self.policy.target), stop=price*(1-self.policy.stop),
                                   next_review='NEXT_SESSION_OR_TARGET_STOP_EVENT')
                    db.execute('INSERT INTO positions VALUES(?,?)', (symbol, json.dumps(payload)))
                else:
                    proceeds = self.costs.sell(price, fill)
                    result['realized_net'] = proceeds-held['cost']*fill/held['qty']
                    db.execute('INSERT INTO settlement VALUES(?,?)', (settlement_date, proceeds))
                    if fill == held['qty']:
                        db.execute('DELETE FROM positions WHERE symbol=?', (symbol,))
                    else:
                        held['cost'] *= (held['qty']-fill)/held['qty']
                        held['qty'] -= fill
                        db.execute('UPDATE positions SET payload=? WHERE symbol=?', (json.dumps(held), symbol))
                result.update(status='FILLED' if fill == qty else 'PARTIAL_REMAINDER_CANCELLED',
                              filled_qty=fill, price=price, execution='OBSERVED_BOOK_PROXY')
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                result['reason'] = str(exc)
            db.execute('UPDATE state SET cash=?,peak=? WHERE id=1', (cash, peak))
            db.execute('INSERT INTO events VALUES(?,?,?)', (decision_id, at, json.dumps(result)))
            return result


def exit_signal(position, quote, held_sessions, policy, history_closes=()):
    """Hard max duration; extensions beyond 20 sessions are never automatic."""
    bid = quote.get('bid')
    if bid is None:
        return 'WAIT_DATA', position['stop']
    stop = position['stop']
    if policy.strategy == 'B':
        stop = max(stop, bid*(1-policy.stop))
    if held_sessions >= policy.max_days:
        return 'SELL_TIME_REEVALUATION', stop
    if bid <= stop:
        return 'SELL_STOP', stop
    if bid >= position['target']:
        return 'SELL_TARGET', stop
    if policy.strategy == 'B' and len(history_closes) >= 5 and bid < sum(history_closes[-5:])/5:
        return 'SELL_TREND_FAILURE', stop
    return 'HOLD_TREND_VALID_REVIEW_NEXT_SESSION', stop
