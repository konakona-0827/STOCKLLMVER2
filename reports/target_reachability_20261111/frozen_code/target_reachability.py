"""Past-only, horizon-specific price space independent of the chosen stop.

The range is anchored to the last completed close. Raising today's entry or
moving its stop cannot increase it. Excursions are observations, not calibrated
target-first probabilities; subsequent execution and entry checks still apply.
"""
import json
import math
from statistics import median

from config import CODE_ROOT


def policy():
    value = json.loads((CODE_ROOT/'config/target_reachability.json').read_text('utf-8-sig'))
    if (type(value['history_bars']) is not int or not 100 <= value['history_bars'] <= 250
            or type(value['minimum_windows']) is not int or value['minimum_windows'] < 4
            or value['favorable_excursion_quantile'] != .5):
        raise ValueError('INVALID_TARGET_REACHABILITY_POLICY')
    return value


def atr_at(bars, index):
    values = [max(float(bars[i]['high'])-float(bars[i]['low']),
                  abs(float(bars[i]['high'])-float(bars[i-1]['close'])),
                  abs(float(bars[i]['low'])-float(bars[i-1]['close'])))
              for i in range(max(1,index-13),index+1)]
    return sum(values)/len(values) if len(values) == 14 else None


def trend_at(bars, index):
    closes = [float(b['close']) for b in bars[index-19:index+1]]
    return (len(closes) == 20 and sum(closes[-5:])/5 > sum(closes)/20
            and closes[-1] >= sum(closes[-5:])/5 and closes[-1] >= closes[-6])


def profile(bars, *, as_of, holding_days, trend, config=None, session_dates=None):
    p = config or policy()
    if type(holding_days) is not int or not 1 <= holding_days <= 22:
        raise ValueError('INVALID_REACHABILITY_HORIZON')
    past = sorted((b for b in bars if b.get('complete') is True and
        b.get('source') in ('TWSE_DAILY','BROKER_DAILY') and b['date'] < as_of), key=lambda b:b['date'])[-p['history_bars']:]
    if len({b['date'] for b in past}) != len(past):
        raise ValueError('DUPLICATE_REACHABILITY_HISTORY')
    for b in past:
        if (any(type(b.get(k)) not in (int,float) or not math.isfinite(b[k]) or b[k] <= 0
                for k in ('open','high','low','close')) or
                not b['low'] <= min(b['open'],b['close']) <= max(b['open'],b['close']) <= b['high']):
            raise ValueError('INVALID_REACHABILITY_HISTORY')
    result = dict(version=p['version'], status='INSUFFICIENT_PAST_WINDOWS',
        scope=p['scope'], holding_days=holding_days, completed_bars=len(past),
        minimum_windows=p['minimum_windows'], target_probability=None, windows=[])
    if not past:
        return result
    if session_dates is not None and not session_dates:
        return dict(result,status='MARKET_CALENDAR_UNVERIFIED')
    result['calendar_validation']='TWSE_ACTUAL_SESSIONS' if session_dates is not None else 'SUPPLIED_BAR_SEQUENCE'
    if session_dates is not None:
        lookup={b['date']:b for b in past}
        dates=sorted({d for d in session_dates if d<as_of})[-p['history_bars']:]
        sequence=[lookup.get(d) for d in dates]
    else:
        sequence=past
    # Work backwards in disjoint forward windows. Every endpoint precedes t0;
    # no overlapping return windows are advertised as independent observations.
    windows = []
    for anchor in range(len(sequence)-1-holding_days, 19, -holding_days):
        if any(b is None for b in sequence[anchor-19:anchor+holding_days+1]):
            continue
        volatility = atr_at(sequence,anchor)
        if not volatility or volatility <= 0:
            continue
        later = sequence[anchor+1:anchor+holding_days+1]
        windows.append(dict(anchor_date=sequence[anchor]['date'], end_date=later[-1]['date'],
            trend=trend_at(sequence,anchor),
            favorable_excursion_atr=(max(float(b['high']) for b in later)-float(sequence[anchor]['close']))/volatility))
    conditional = [w for w in windows if w['trend'] == bool(trend)]
    cohort = conditional if len(conditional) >= p['minimum_windows'] else windows
    result.update(available_windows=len(windows), matching_regime_windows=len(conditional),
        conditioning='SAME_COMPLETED_TREND_REGIME' if cohort is conditional else 'VOLATILITY_RANGE_ONLY',
        windows=cohort, anchor_date=past[-1]['date'], anchor_close=float(past[-1]['close']))
    if len(cohort) < p['minimum_windows']:
        return result
    if any(b is None for b in sequence[-15:]):
        return dict(result,status='RECENT_HISTORY_GAP')
    volatility = atr_at(sequence,len(sequence)-1)
    if not volatility or volatility <= 0:
        return result
    excursion = median(w['favorable_excursion_atr'] for w in cohort)
    result.update(status='AVAILABLE', favorable_excursion_atr=excursion, current_atr14=volatility,
        price_ceiling=float(past[-1]['close'])+excursion*volatility)
    return result


def view(profile):
    """Small shared evidence; raw historical windows stay in the audit/tool."""
    return {k:v for k,v in profile.items() if k != 'windows'}
