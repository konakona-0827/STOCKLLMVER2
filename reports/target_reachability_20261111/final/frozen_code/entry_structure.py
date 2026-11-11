"""Point-in-time resistance and stop provenance shared by planning and research.

An observed barrier is a scenario boundary, not an estimated success probability.
Short recoveries must first clear the last completed session's high. Longer trend
plans retain swing resistance; arbitrary daily highs are not all hard exits.
"""
from decimal import Decimal

VERSION = 'entry-structure-v1'


def completed_recovery(prior, signal, prior_change):
    """Name the observed reclaim and the levels to check on a later retest."""
    try:
        low, high = Decimal(str(signal['low'])), Decimal(str(signal['high']))
        close, reclaimed = Decimal(str(signal['close'])), Decimal(str(prior['high']))
        opening = Decimal(str(signal['open']))
    except (KeyError, ArithmeticError):
        return None
    if (not all(price.is_finite() and price > 0 for price in (low, high, close, reclaimed, opening))
            or prior_change >= 0 or high <= low or close <= reclaimed
            or close <= opening or (close-low)/(high-low) < Decimal('.75')):
        return None
    return dict(kind='COMPLETED_REBOUND_RETEST', signal_session=signal['date'],
                prior_session=prior['date'], reclaimed_high=float(reclaimed),
                observed_target=float(high), daily_invalidation=float(low))


def resistance_levels(bars, levels, *, recovery):
    result = []
    for level in levels:
        name = level['id']
        if name.startswith(('high_', 'swing_high_')):
            result.append(dict(price=float(level['price']), kind='OBSERVED_SWING_RESISTANCE',
                               source=name, date=level.get('basis')))
    if recovery:
        latest = bars[-1]
        result.append(dict(price=float(latest['high']), kind='OBSERVED_RECENT_SESSION_HIGH',
                           source='last_completed_high', date=latest['date']))
    # A swing low becomes overhead resistance only after a LATER completed
    # close broke it. Earlier lows cannot invalidate a newly formed support.
    for index in range(1, len(bars) - 1):
        bar = bars[index]
        if bar['low'] <= bars[index-1]['low'] and bar['low'] < bars[index+1]['low']:
            broken = next((b for b in bars[index+1:] if b['close'] < bar['low']), None)
            if broken:
                result.append(dict(price=float(bar['low']), kind='OBSERVED_BROKEN_SUPPORT_RESISTANCE',
                    source='broken_swing_low', date=bar['date'], broken_on=broken['date']))
    # Prefer the specific provenance when prices overlap.
    return sorted(result, key=lambda r: (r['price'], r['kind'] == 'OBSERVED_SWING_RESISTANCE'))


def target_evidence(levels, entry):
    return next((r for r in levels if Decimal(str(r['price'])) > entry), None)


def stop_evidence(bars, stop, today_low, session):
    if stop == Decimal(str(today_low)):
        return dict(kind='LIVE_TODAY_LOW', formed_on=session)
    matches = [b for b in bars if Decimal(str(b['low'])) == stop]
    formed = matches[-1]['date'] if matches else None
    broken = next((b['date'] for b in bars if formed and b['date'] > formed
                   and Decimal(str(b['close'])) < stop), None)
    return dict(kind='COMPLETED_SUPPORT', formed_on=formed, broken_on=broken)
