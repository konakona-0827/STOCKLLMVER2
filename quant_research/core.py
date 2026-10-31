"""Point-in-time factors and purged historical conditional estimates.

Daily-bar simulation is deliberately next-open only. A high touching a target
never implies a fill. A/B use identical execution and cost assumptions.
"""
from dataclasses import dataclass, asdict, replace
from datetime import datetime
from math import isfinite, sqrt, floor, ceil
from statistics import mean, pstdev
import hashlib
import json

VERSION = 'quant-ab-1.0.0'
MODEL = 'purged-conditional-v1'
RISK = 'isolated-risk-v1'
PROMPT = 'event-only-no-llm-v1'


@dataclass(frozen=True)
class Costs:
    commission: float = .001425
    minimum: float = 20
    tax: float = .003
    slippage: float = .001

    def __post_init__(self):
        if any(not isfinite(v) or v < 0 for v in asdict(self).values()):
            raise ValueError('INVALID_COSTS')

    def fee(self, gross):
        return max(self.minimum, floor(gross * self.commission))

    def buy(self, price, qty):
        gross = price * (1 + self.slippage) * qty
        return gross + self.fee(gross)

    def sell(self, price, qty):
        gross = price * (1 - self.slippage) * qty
        return gross - self.fee(gross) - ceil(gross * self.tax)

    def pnl(self, entry, exit_price, qty):
        return self.sell(exit_price, qty) - self.buy(entry, qty)


@dataclass(frozen=True)
class Policy:
    strategy: str
    target: float = .015
    stop: float = .012
    max_days: int = 5
    max_positions: int = 10
    position_fraction: float = .10
    sector_fraction: float = .30
    risk_fraction: float = .005
    drawdown_limit: float = .10
    min_samples: int = 30
    participation: float = .001
    min_turnover: float = 10_000_000

    def __post_init__(self):
        if self.strategy not in ('A', 'B', 'BASELINE'):
            raise ValueError('UNKNOWN_STRATEGY')
        if not 0 < self.stop < 1 or not 0 < self.target < 1 or not 1 <= self.max_days <= 20:
            raise ValueError('INVALID_PLAN')
        if not self.max_positions > 0 or not self.min_samples > 0:
            raise ValueError('INVALID_LIMITS')
        for v in (self.position_fraction, self.sector_fraction, self.risk_fraction,
                  self.drawdown_limit, self.participation):
            if not 0 < v <= 1:
                raise ValueError('INVALID_LIMITS')


def validate_bars(rows):
    result, seen = [], set()
    for row in sorted(rows, key=lambda b: b['date']):
        datetime.fromisoformat(row['date'])
        if row['date'] in seen:
            raise ValueError('DUPLICATE_BAR')
        seen.add(row['date'])
        if row.get('source') not in ('TWSE_DAILY', 'BROKER_DAILY', 'SYNTHETIC_TEST') or row.get('complete') is not True:
            raise ValueError('UNCERTIFIED_BAR')
        if not all(type(row.get(k)) in (int, float) and isfinite(row[k]) and row[k] > 0
                   for k in ('open', 'high', 'low', 'close', 'volume')):
            raise ValueError('INVALID_BAR')
        if not row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']:
            raise ValueError('INVALID_OHLC')
        result.append(row)
    return result


def features(rows, at, events=(), benchmark=None):
    # The model uses completed sessions strictly before the decision session.
    session = datetime.fromisoformat(at).date().isoformat()
    past = [r for r in rows if r['date'] < session]
    if len(past) < 61:
        return None
    c = [r['close'] for r in past]
    r = {str(h): c[-1] / c[-h-1] - 1 for h in (5, 10, 20, 60)}
    ma = {str(h): mean(c[-h:]) for h in (5, 10, 20, 60)}
    atr = mean(max(b['high']-b['low'], abs(b['high']-a['close']), abs(b['low']-a['close']))
               for a, b in zip(past[-15:-1], past[-14:])) / c[-1]
    vol = pstdev([c[i]/c[i-1]-1 for i in range(len(c)-20, len(c))])
    visible = [e for e in events if e.get('published_at', '9999') < at and e.get('source')]
    factor = {}
    for e in sorted(visible, key=lambda e: e['published_at']):
        factor[e['kind']] = e
    missing = [k for k in ('revenue', 'earnings', 'industry', 'breadth', 'external') if k not in factor]
    market_return = None
    if benchmark:
        bc = [b['close'] for b in benchmark if b['date'] < session]
        if len(bc) > 20:
            market_return = bc[-1]/bc[-21]-1
    relative = r['20'] - market_return if market_return is not None else None
    breakout = c[-1] > max(b['high'] for b in past[-21:-1])
    false_break = past[-1]['high'] > max(b['high'] for b in past[-21:-1]) and not breakout
    rebound = r['5'] > 0 and c[-1] < max(c[-20:]) * .97
    trend = ma['5'] > ma['20'] > ma['60'] and ma['20'] > mean(c[-25:-5])
    event_risk = any(e.get('risk') == 'BLOCK' for e in factor.values())
    # Missing nontechnical factors contribute zero, never invented numbers.
    score = 2*r['5'] + r['10'] + r['20'] + .5*r['60'] + .05*trend + .03*breakout + .02*rebound - .05*false_break
    score += (relative or 0) * .5
    # Only explicit numeric, published factor values enter ranking.
    contributions = {}
    for kind, event in factor.items():
        value = event.get('score')
        if type(value) in (int, float) and isfinite(value):
            contributions[kind] = max(-1, min(1, value)) * .02
    score += sum(contributions.values())
    return dict(momentum=r, ma=ma, atr=atr, volatility=vol, breakout=breakout,
                false_breakout=false_break, rebound=rebound, trend=trend,
                volume_ratio=mean(b['volume'] for b in past[-5:])/mean(b['volume'] for b in past[-20:]),
                liquidity=mean(b['close']*b['volume'] for b in past[-20:]),
                score=score, relative_strength=relative, regime='UNKNOWN' if market_return is None else
                ('UP' if market_return > 0 else 'DOWN'), events=factor, missing_factors=missing,
                event_risk=event_risk, event_contributions=contributions,
                as_of=at, last_bar=past[-1]['date'], source=past[-1]['source'],
                bucket=('trend' if trend else 'rebound' if rebound else 'breakout' if breakout else 'other'))


def outcome(rows, start, policy, costs, qty=100):
    """Executable research policy: next open after a close signal, never same-bar TP.

    Gap losses are uncapped. Both-barrier daily ambiguity counts stop-first in
    labels; accounting uses the *following* observed open with slippage.
    """
    path = rows[start:start+policy.max_days+1]
    if len(path) != policy.max_days+1:
        return None
    entry = path[0]['open']
    target, stop, peak = entry*(1+policy.target), entry*(1-policy.stop), entry
    target_day, stop_first, exit_index, reason = None, False, policy.max_days, 'TIME_LIMIT'
    adverse = 0
    for i, bar in enumerate(path[:policy.max_days]):
        adverse = min(adverse, bar['low']/entry-1)
        # No same-session sell: policy is valid for the existing odd-lot interface.
        hit_stop, hit_target = bar['low'] <= stop, bar['high'] >= target
        if hit_stop or hit_target:
            stop_first = hit_stop
            target_day = i+1 if hit_target and not hit_stop else None
            exit_index, reason = i+1, 'STOP_SIGNAL' if hit_stop else 'TARGET_SIGNAL'
            break
        if policy.strategy == 'B':
            peak = max(peak, bar['close'])
            stop = max(stop, peak*(1-policy.stop))
            if i >= 4 and bar['close'] < mean(b['close'] for b in path[max(0, i-4):i+1]):
                exit_index, reason = i+1, 'TREND_FAILURE'
                break
    exit_price = path[exit_index]['open']
    return dict(tp_day=target_day, stop_first=stop_first, days=exit_index,
                net=costs.pnl(entry, exit_price, qty), entry=entry, exit=exit_price,
                adverse=adverse, reason=reason, end=path[exit_index]['date'])


def predict(rows, at, policy, costs, qty=100):
    at = datetime.fromisoformat(at).date().isoformat()
    f = features(rows, at)
    samples, last_end = [], ''
    if f:
        for i in range(61, len(rows)-policy.max_days):
            # Purge all outcomes not fully known before this decision.
            if rows[i+policy.max_days]['date'] >= at or rows[i]['date'] <= last_end:
                continue
            x = features(rows[:i], rows[i]['date'])
            if x and x['bucket'] == f['bucket']:
                y = outcome(rows, i, policy, costs, qty)
                samples.append(y)
                last_end = y['end']
    n = len(samples)
    base = dict(model_version=MODEL, sample_count=n, independent_nonoverlapping=True,
                training_end=max((s['end'] for s in samples), default=None), calibrated=False,
                status='INSUFFICIENT_EVIDENCE', horizons={}, expected_net=None,
                expected_days=None, adverse=None)
    if n < policy.min_samples:
        return base
    horizons = {}
    for h in ((3, 5) if policy.strategy == 'A' else (5, 10, 20)):
        if h > policy.max_days:
            continue
        p = mean(s['tp_day'] is not None and s['tp_day'] <= h for s in samples)
        # Wilson interval: expresses uncertainty; not a claim of calibration.
        z, den = 1.96, 1+1.96**2/n
        center = (p+z*z/(2*n))/den
        width = z*sqrt(p*(1-p)/n+z*z/(4*n*n))/den
        horizons[str(h)] = dict(tp_first=p, interval=[max(0, center-width), min(1, center+width)])
    reference = next(r['close'] for r in reversed(rows) if r['date'] < at)
    # Apply historical path returns to today's price/quantity and fee floor.
    net = [costs.pnl(reference, reference*s['exit']/s['entry'], qty) for s in samples]
    base.update(status='ESTIMATED_UNCALIBRATED', horizons=horizons,
                expected_net=mean(net), net_lower=mean(net)-1.96*pstdev(net)/sqrt(n),
                expected_net_return=mean(net)/costs.buy(reference, qty),
                downside_net_return=sorted(net)[max(0, int(n*.05)-1)]/costs.buy(reference, qty),
                trend_continuation_proxy=mean(s['exit'] > s['entry'] for s in samples),
                expected_days=mean(s['days'] for s in samples),
                stop_first=mean(s['stop_first'] for s in samples), adverse=min(s['adverse'] for s in samples))
    return base


def multi_horizon(rows, at, policy, costs, qty):
    """Separate purged path outcomes for each horizon, including timeout exits."""
    result = {}
    for horizon in ((3, 5) if policy.strategy == 'A' else (5, 10, 20)):
        p = replace(policy, max_days=horizon)
        result[str(horizon)] = predict(rows, at, p, costs, qty)
    return result


def identity(policy, costs):
    return dict(strategy_version=VERSION, model_version=MODEL, risk_version=RISK,
                prompt_version=PROMPT, config_sha256=hashlib.sha256(json.dumps(
                    [asdict(policy), asdict(costs)], sort_keys=True).encode()).hexdigest())
