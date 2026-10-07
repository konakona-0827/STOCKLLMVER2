"""Price/time helpers and legacy snapshot parser; no network client."""
from datetime import datetime
import math
from config import TAIPEI, MAX_QUOTE_AGE_SECONDS, now



def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (TypeError, ValueError):
        return None


def parse_quote(item, at):
    stamp = datetime.strptime(item['d'] + ' ' + item['t'], '%Y%m%d %H:%M:%S').replace(tzinfo=TAIPEI)
    bid = number(str(item.get('b', '')).split('_')[0])
    ask = number(str(item.get('a', '')).split('_')[0])
    # Last trade can be absent in a snapshot. Preserve None instead of inventing it.
    price = number(item.get('z'))
    return dict(symbol=item['c'], name=item.get('n', ''), exchange_time=stamp.isoformat(),
                fetched_at=at.isoformat(), price=price, bid=bid, ask=ask,
                volume_lots=number(item.get('v')) or 0,
                previous_close=number(item.get('y')), source='TWSE_MIS',
                quote_basis='regular_board_not_odd_lot',
                quality=quality(stamp.isoformat(), at),
                evidence_id=f"TWSE:{item['c']}:{stamp.isoformat()}")


def quality(stamp, at):
    age = (at - datetime.fromisoformat(stamp)).total_seconds()
    return 'FRESH' if 0 <= age <= MAX_QUOTE_AGE_SECONDS else 'STALE'


def tick(price, side):
    # The step immediately below a threshold differs from the step above it.
    p = price - 0.000001 if side == 'SELL' else price
    for ceiling, step in ((10, .01), (50, .05), (100, .1), (500, .5), (1000, 1)):
        if p < ceiling:
            return step
    return 5


def execution_price(quote, side):
    base = quote.get('ask' if side == 'BUY' else 'bid')
    if not base:
        return None
    return round(base + tick(base, side) * (1 if side == 'BUY' else -1), 2)
