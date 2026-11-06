"""Point-in-time research triggers. A trigger never authorizes an order."""
from datetime import datetime
import math


def observe(watch, quote):
    result = dict(watch, status='NO_LIVE_QUOTE', observed_price=quote.get('last_price'),
                  quote_timestamp=quote.get('quote_timestamp'))
    try:
        stamp = datetime.fromisoformat(quote['quote_timestamp'])
        if stamp.tzinfo is None or watch['expires_session'] != stamp.date().isoformat():
            return {**result, 'status':'EXPIRED_WATCH'}
        price, level = quote['last_price'], watch['condition_price']
        if (quote.get('quote_status') != 'LIVE' or quote.get('is_trial') or
                any(type(v) not in (int,float) or not math.isfinite(v) or v <= 0 for v in (price,level))):
            return result
        kind = watch['trigger_kind']
        if kind not in ('PULLBACK_TO_CLOSE','BREAK_ABOVE_HIGH','RECLAIM_OPEN'):
            return result
        touched = price <= level if kind == 'PULLBACK_TO_CLOSE' else (
            price > level if kind == 'BREAK_ABOVE_HIGH' else price >= level)
        result['status'] = 'RESEARCH_CONDITION_MET' if touched else 'WAITING_FOR_CONDITION'
    except (KeyError,TypeError,ValueError):
        pass
    return result
