"""Completed-bar opening-range trigger. Never reconstruct minute bars from ticks."""
from datetime import datetime, timedelta

from .market import Market, decimal, instant


def opening_range_signal(bars, at, sessions, *, range_minutes=15, interval_minutes=5, minimum_volume_multiple=1.5):
    market = Market()
    clock = instant(at).astimezone(market.tz)
    day = clock.date().isoformat()
    if not market.session_open(at, sessions):
        return dict(status="OUTSIDE_SESSION", trigger=None)
    if range_minutes <= 0 or interval_minutes <= 0 or range_minutes % interval_minutes:
        raise ValueError("INVALID_RANGE_OR_INTERVAL")
    start = datetime.fromisoformat(day + "T" + market.rules["regular_open"]).replace(tzinfo=market.tz)
    step = timedelta(minutes=interval_minutes)
    closed, symbols = {}, set()
    for row in bars:
        timestamp = instant(row["start"]).astimezone(market.tz)
        end = timestamp + step
        if end > clock:
            continue  # Partial bars cannot trigger or set the range high.
        if instant(row["received_at"]) > clock or instant(row["available_at"]) > clock:
            continue
        if timestamp.date().isoformat() != day or timestamp < start:
            continue
        offset = (timestamp - start).total_seconds()
        if offset % step.total_seconds():
            raise ValueError("OFF_GRID_BAR")
        slot = int(offset // step.total_seconds())
        if slot in closed:
            raise ValueError("DUPLICATE_BAR")
        symbols.add(row["symbol"])
        prices = [decimal(row[k]) for k in ("open", "high", "low", "close")]
        o, h, l, c = prices
        if not 0 < l <= min(o, c) <= max(o, c) <= h or decimal(row["volume"]) <= 0:
            raise ValueError("INVALID_BAR")
        closed[slot] = row
    if len(symbols) > 1:
        raise ValueError("MIXED_SYMBOLS")
    n = range_minutes // interval_minutes
    if any(i not in closed for i in range(n)):
        return dict(status="INCOMPLETE_OPENING_RANGE", trigger=None)
    high = max(decimal(closed[i]["high"]) for i in range(n))
    low = min(decimal(closed[i]["low"]) for i in range(n))
    average_volume = sum(decimal(closed[i]["volume"]) for i in range(n)) / n
    for i in range(n, max(closed) + 1):
        if i not in closed:
            return dict(status="GAPPED_TRIGGER_HISTORY", trigger=None)
        row = closed[i]
        if decimal(row["close"]) > high and decimal(row["volume"]) >= average_volume * decimal(minimum_volume_multiple):
            return dict(status="TRIGGERED_RESEARCH", trigger=dict(symbol=row["symbol"], close=row["close"],
                        closed_at=(instant(row["start"]) + step).isoformat(), evidence_id=row["evidence_id"]),
                        opening_range_high=str(high), opening_range_low=str(low))
    return dict(status="WAITING", trigger=None, opening_range_high=str(high), opening_range_low=str(low))
