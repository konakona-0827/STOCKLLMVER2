"""Deterministic snapshot-only research score. No execution or price prediction."""
from datetime import datetime
import math


def finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def clip(v, lo=0, hi=1):
    return min(hi, max(lo, v))


def scan_quote(q, rules, at):
    last, op, high, low = [q.get(k) for k in ('last_price', 'open', 'high', 'low')]
    bid, ask, volume = q.get('bid'), q.get('ask'), q.get('volume')
    reasons, warnings = [], list(q.get('warnings', []))
    if q['quote_status'] == 'UNAVAILABLE':
        reasons.append('UNAVAILABLE')
    if not finite(last) or last <= 0:
        reasons.append('INVALID_LAST_PRICE')
    valid_book = finite(bid) and finite(ask) and 0 < bid <= ask
    if not valid_book:
        reasons.append('INVALID_BID_ASK')
    if not finite(volume) or volume < rules['min_volume_shares']:
        reasons.append('LOW_OR_MISSING_VOLUME')
    if q.get('is_trial'):
        reasons.append('TRIAL_QUOTE')
    valid_last = finite(last) and last > 0
    intraday_return = last / op - 1 if valid_last and finite(op) and op > 0 else None
    range_position = None
    if valid_last and finite(high) and finite(low) and 0 < low < high and low <= last <= high:
        range_position = (last-low)/(high-low)
    if intraday_return is None:
        warnings.append('MISSING_OPEN_MOMENTUM_UNAVAILABLE')
    if range_position is None:
        warnings.append('MISSING_FLAT_OR_INVALID_RANGE')
    spread = ask - bid if valid_book else None
    spread_pct = spread / last if spread is not None and valid_last else None
    if spread_pct is not None and spread_pct > rules['max_spread_pct']:
        reasons.append('SPREAD_TOO_WIDE')
    turnover = last * volume if valid_last and finite(volume) and volume >= 0 else None
    if turnover is None or turnover < rules['min_turnover_proxy_twd']:
        reasons.append('LOW_LIQUIDITY_PROXY')
    age = None
    try:
        age = (at-datetime.fromisoformat(q['quote_timestamp'])).total_seconds()
        if age < 0:
            reasons.append('FUTURE_QUOTE')
    except (KeyError, TypeError, ValueError):
        reasons.append('INVALID_QUOTE_TIME')
    metrics = dict(intraday_return=intraday_return, range_position=range_position,
                   spread=spread, spread_pct=spread_pct, volume=volume,
                   freshness_seconds=age, liquidity_proxy_twd=turnover,
                   liquidity_proxy_definition='last_price * cumulative_odd_lot_volume_not_actual_turnover',
                   history_status='INSUFFICIENT_VALIDATED_DAILY_BARS',
                   return_1d=None, return_5d=None, return_20d=None, volume_ratio=None,
                   MA5=None, MA20=None, price_vs_MA5=None, price_vs_MA20=None,
                   RSI=None, ATR=None, high_20d_distance=None, breakout_score=None,
                   drawdown_from_recent_high=None, volatility=None)
    # Stored snapshots are not certified daily OHLC bars. Never reinterpret
    # repeated intraday/closing snapshots as 5 or 20 days of observations.
    w = rules['weights']
    components = dict(
        momentum=w['momentum'] * clip((intraday_return or 0)/rules['momentum_return_scale'], -1, 1),
        volume=w['volume'] * clip(math.log1p(max(0, volume)) / math.log1p(rules['volume_reference_shares'])) if finite(volume) else 0,
        breakout=w['breakout'] * (range_position if range_position is not None else 0),
        liquidity=w['liquidity'] * clip(math.log1p(max(0, turnover))/math.log1p(rules['liquidity_reference_twd'])) if turnover is not None else 0,
        spread_penalty=-w['spread_penalty'] * clip(spread_pct/rules['max_spread_pct']) if spread_pct is not None else -w['spread_penalty'],
        stale_penalty=-w['stale_penalty'] if q['quote_status'] == 'LAST_KNOWN' else 0)
    components = {k: round(v, 6) for k, v in components.items()}
    return dict(symbol=q['symbol'], quote_status=q['quote_status'], eligible=not reasons,
                exclusion_reasons=reasons, warnings=warnings, metrics=metrics,
                components=components, quant_score=round(sum(components.values()), 6) if not reasons else None,
                score_version=rules['version'],
                component_note='breakout component is intraday range position, not historical breakout')


def scan_and_rank(quotes, rules, at):
    scanned = [scan_quote(q, rules, at) for q in quotes]
    ranked = sorted((q for q in scanned if q['eligible']), key=lambda q: (-q['quant_score'], q['symbol']))
    ranking = [dict(rank=i, **q) for i, q in enumerate(ranked, 1)]
    return scanned, ranking, ranking[:rules['candidate_top_n']]
