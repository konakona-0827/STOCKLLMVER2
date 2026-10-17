"""Point-in-time levels and deterministic entry guards for AI advice."""
from datetime import date, datetime
from decimal import Decimal
import math

from market_calendar import previous_trading_day
from trade_economics import evaluate_buy

MIN_NET_RETURN = Decimal('0.02')
MIN_REWARD_RISK = Decimal('1.5')
MAX_POSITION_RISK = Decimal('0.005')
REENTRY_COOLDOWN_DAYS = 3


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def price_levels(candidate):
    """Only verified past daily bars and this live quote may supply levels."""
    try:
        session = datetime.fromisoformat(candidate['quote_timestamp']).date()
    except (KeyError, TypeError, ValueError):
        return []
    rows = {}
    for bar in candidate.get('daily_bars') or []:
        if not isinstance(bar, dict) or bar.get('source') not in ('TWSE_DAILY', 'BROKER_DAILY') or bar.get('complete') is not True:
            continue
        try:
            day = date.fromisoformat(bar['date'])
        except (KeyError, TypeError, ValueError):
            continue
        if day >= session or not all(_positive(bar.get(k)) for k in ('high', 'low', 'close')):
            continue
        if not bar['low'] <= bar['close'] <= bar['high'] or day in rows:
            continue
        rows[day] = bar
    levels = []
    yesterday = previous_trading_day(session)
    if yesterday in rows:
        levels.append(dict(id='previous_close', price=rows[yesterday]['close'], basis=yesterday.isoformat()))
    for window in (5, 10, 20):
        days, cursor = [], session
        for _ in range(window):
            cursor = previous_trading_day(cursor)
            if cursor not in rows:
                break
            days.append(cursor)
        if len(days) == window:
            levels.extend((
                dict(id=f'high_{window}d', price=max(rows[d]['high'] for d in days), basis=f'{days[-1]}..{days[0]}'),
                dict(id=f'low_{window}d', price=min(rows[d]['low'] for d in days), basis=f'{days[-1]}..{days[0]}'),
            ))
    intraday = all(_positive(candidate.get(key)) for key in
                   ('open', 'high', 'low', 'last_price', 'bid', 'ask'))
    intraday = (intraday and candidate['low'] <= candidate['open'] <= candidate['high']
                and candidate['low'] <= candidate['last_price'] <= candidate['high']
                and candidate['bid'] <= candidate['ask'])
    if candidate.get('quote_status') == 'LIVE' and not candidate.get('is_trial') and intraday:
        for key in ('open', 'high', 'low'):
            if _positive(candidate.get(key)):
                levels.append(dict(id='today_' + key, price=candidate[key], basis=session.isoformat()))
    return levels


def supported_price(levels, price):
    try:
        proposed = Decimal(str(price))
        return proposed.is_finite() and any(proposed == Decimal(str(row['price'])) for row in levels)
    except (TypeError, ValueError, ArithmeticError):
        return False


def cooldown_active(records, session, days=REENTRY_COOLDOWN_DAYS):
    if not isinstance(records, list):
        return False
    prior = {previous_trading_day(session)}
    cursor = next(iter(prior))
    for _ in range(days - 1):
        cursor = previous_trading_day(cursor)
        prior.add(cursor)
    for row in records:
        if row.get('side') != 'SELL' or int(row.get('quantity') or 0) <= 0:
            continue
        try:
            sold = date.fromisoformat(row['date'])
        except (KeyError, TypeError, ValueError):
            continue
        if sold == session or sold in prior:
            return True
    return False


def buy_block(candidate):
    held = int((candidate.get('paper_position') or {}).get('qty') or 0)
    if held:
        basis = candidate.get('ai_cost_basis') or {}
        cost = basis.get('average_unit_cost_twd') if basis.get('available') else None
        if cost is None:
            cents = (candidate.get('paper_position') or {}).get('cost_cents')
            cost = Decimal(str(cents)) / (100 * held) if cents else None
        bid = candidate.get('bid')
        if cost is None or not _positive(bid):
            return 'BUY_EXISTING_POSITION_COST_UNKNOWN'
        if Decimal(str(bid)) < Decimal(str(cost)):
            return 'BUY_LOSING_POSITION_ADD_BLOCKED'
    try:
        session = datetime.fromisoformat(candidate['quote_timestamp']).date()
    except (KeyError, TypeError, ValueError):
        return 'BUY_SESSION_UNKNOWN'
    if cooldown_active((candidate.get('ai_trade_dates') or {}).get('records'), session):
        return 'BUY_REENTRY_COOLDOWN'
    return None


def risk_sized_qty(candidate, decision, capital_twd, *, minimum_commission_twd, commission_rate):
    """Reduce requested shares until modeled loss fits the configured risk budget."""
    if capital_twd is None or Decimal(str(capital_twd)) <= 0:
        return 0, None
    budget = Decimal(str(capital_twd)) * MAX_POSITION_RISK
    requested = min(decision['suggested_qty'], candidate.get('max_buy_qty', decision['suggested_qty']))
    for qty in range(requested, 0, -1):
        economics = evaluate_buy(candidate, qty=qty, limit_price=decision['suggested_price'],
                                 target_price=decision['target_price'],
                                 invalidation_price=decision['invalidation_price'],
                                 minimum_commission_twd=minimum_commission_twd,
                                 commission_rate=commission_rate)
        if Decimal(str(economics['estimated_net_downside_twd'])) <= budget:
            return qty, economics
    return 0, None
