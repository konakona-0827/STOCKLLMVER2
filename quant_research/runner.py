"""Offline replay/forward snapshot runner. Never connects to broker or LLM.

Each invocation creates an immutable experiment directory; shadow trades have
their own ledger. Cached data are unadjusted: corporate-action risk is explicit.
"""
from dataclasses import asdict, replace
from pathlib import Path
from statistics import mean
import argparse
import collections
import hashlib
import json
import subprocess
import uuid

from .core import Costs, Policy, features, predict, outcome, validate_bars, identity


def load_cache(path):
    by_symbol = collections.defaultdict(dict)
    for file in sorted(Path(path).glob('*.json')):
        payload = json.loads(file.read_text(encoding='utf-8-sig'))
        for bar in payload.get('bars', []):
            old = by_symbol[payload['symbol']].get(bar['date'])
            if old and any(old[k] != bar[k] for k in ('open', 'high', 'low', 'close', 'volume')):
                raise ValueError('CONFLICTING_BAR: '+payload['symbol']+' '+bar['date'])
            by_symbol[payload['symbol']][bar['date']] = bar
    if len(by_symbol) > 1000:
        raise ValueError('CAPACITY_LIMIT_1000')
    return {s: validate_bars(list(b.values())) for s, b in by_symbol.items()
            if len(s) == 4 and not s.startswith('00')}  # ordinary equities only


class RiskEngine:
    def check(self, symbol, sector, qty, price, cash, equity, peak, positions, policy, costs):
        if qty < 1 or qty > 999:
            return 'ODDLOT_SIZE'
        if symbol in positions:
            return 'ALREADY_HELD'
        if len(positions) >= policy.max_positions:
            return 'POSITION_LIMIT'
        if equity <= peak*(1-policy.drawdown_limit):
            return 'DRAWDOWN_LIMIT'
        buy = costs.buy(price, qty)
        if buy > cash:
            return 'SETTLED_CASH'
        if buy > equity*policy.position_fraction:
            return 'SINGLE_NAME_LIMIT'
        sector_cost = sum(p['cost'] for p in positions.values() if p['sector'] == sector)
        if sector_cost + buy > equity*policy.sector_fraction:
            return 'SECTOR_LIMIT'
        if -costs.pnl(price, price*(1-policy.stop), qty) > equity*policy.risk_fraction:
            return 'LOSS_BUDGET'
        return None


def metrics(trades, equity, initial, decisions):
    pnl = [t['net'] for t in trades]
    wins, losses = sum(max(0, n) for n in pnl), -sum(min(0, n) for n in pnl)
    peak, dd = initial, 0
    for e in equity:
        peak = max(peak, e['equity'])
        dd = max(dd, 1-e['equity']/peak)
    # Prequential score: only predictions made before each later outcome.
    scores = []
    for t in trades:
        h = t['prediction'].get('horizons', {})
        if h:
            key = max(h, key=int)
            scores.append((h[key]['tp_first'] - int(t['tp_day'] is not None))**2)
    return dict(closed_trades=len(trades), average_net=mean(pnl) if pnl else None,
                win_rate=mean(n > 0 for n in pnl) if pnl else None,
                profit_factor=wins/losses if losses else None,
                max_drawdown=dd, net_asset_return=equity[-1]['equity']/initial-1 if equity else 0,
                average_holding_days=mean(t['days'] for t in trades) if trades else None,
                net_per_capital_day=sum(pnl)/sum(t['cost']*t['days'] for t in trades) if trades else None,
                tp_signal_rates={str(h): mean(t['tp_day'] is not None and t['tp_day'] <= h for t in trades)
                                 if trades else None for h in (3, 5, 10, 20)},
                brier=mean(scores) if scores else None, calibration_samples=len(scores),
                funnel=dict(collections.Counter(d['reason'] for d in decisions)),
                daily_candidates=dict(collections.Counter(d['at'] for d in decisions if d.get('quant_pass'))))


def replay(data, policy, costs=Costs(), initial=100000, sectors=None, events=None, start=None, end=None):
    data = {s: validate_bars(b) for s, b in data.items()}
    sectors, events = sectors or {}, events or {}
    dates = sorted({b['date'] for bars in data.values() for b in bars
                    if (not start or b['date'] >= start) and (not end or b['date'] <= end)})
    # Settlement sessions come from observed market dates; no guessed weekdays.
    calendar = sorted({b['date'] for bars in data.values() for b in bars})
    index = {s: {b['date']: i for i, b in enumerate(bars)} for s, bars in data.items()}
    cash, peak, positions, settlement = float(initial), float(initial), {}, []
    decisions, trades, shadows, curve, forecasts = [], [], [], [], []
    guard = RiskEngine()
    for at in dates:
        due = [x for x in settlement if x[0] <= at]
        cash += sum(x[1] for x in due)
        settlement = [x for x in settlement if x[0] > at]
        for symbol, p in list(positions.items()):
            if p['result'] and p['result']['end'] == at:
                y = p['result']
                proceeds = costs.sell(y['exit'], p['qty'])
                ci = calendar.index(at)
                settle = calendar[ci+2] if ci+2 < len(calendar) else '9999-12-31'
                settlement.append((settle, proceeds))
                trades.append(dict(symbol=symbol, entry_at=p['at'], exit_at=at,
                                   qty=p['qty'], cost=p['cost'], prediction=p['prediction'], **y))
                del positions[symbol]
        def mark(symbol, p):
            observed = [b for b in data[symbol] if b['date'] <= at]
            return costs.sell(observed[-1]['close'], p['qty']) if observed else p['cost']
        equity = cash + sum(v for _, v in settlement) + sum(mark(s, p) for s, p in positions.items())
        peak = max(peak, equity)
        ranked = []
        for symbol, rows in data.items():
            if at not in index[symbol]:
                continue
            i = index[symbol][at]
            f = features(rows, at, events.get(symbol, []), data.get('0050'))
            d = dict(at=at, symbol=symbol, strategy=policy.strategy, quant_pass=False,
                     reason='INSUFFICIENT_HISTORY', features=f)
            decisions.append(d)
            if not f:
                continue
            if f['event_risk']:
                d['reason'] = 'EVENT_RISK'
                continue
            if f['liquidity'] < policy.min_turnover:
                d['reason'] = 'LIQUIDITY'
                continue
            signal = f['bucket'] != 'other' if policy.strategy == 'A' else f['trend'] or f['breakout']
            if not signal:
                d['reason'] = 'NO_SIGNAL'
                continue
            ref = rows[i-1]['close']
            max_qty = min(999, int(equity*policy.position_fraction/ref),
                          int(rows[i-1]['volume']*policy.participation))
            # Costs, stop risk and minimum commission constrain the actual sized quantity.
            while max_qty and (costs.buy(ref, max_qty) > equity*policy.position_fraction or
                              -costs.pnl(ref, ref*(1-policy.stop), max_qty) > equity*policy.risk_fraction):
                max_qty -= 1
            model = predict(rows, at, policy, costs, max(1, max_qty))
            d.update(prediction=model, qty=max_qty)
            if policy.strategy != 'BASELINE' and model['expected_net'] is None:
                d['reason'] = 'INSUFFICIENT_EVIDENCE'
                continue
            if policy.strategy != 'BASELINE' and model['net_lower'] <= 0:
                d['reason'] = 'NET_EDGE_UNCERTAIN'
                continue
            d['quant_pass'] = True
            d['reason'] = 'QUANT_PASS'
            utility = (model.get('expected_net') or 0) / max(1, costs.buy(ref, max(1, max_qty))) / (model.get('expected_days') or policy.max_days)
            ranked.append((utility, f['score'], symbol, d, i))
        for _, _, symbol, d, i in sorted(ranked, key=lambda x: (-x[0], -x[1], x[2])):
            rows, qty = data[symbol], d['qty']
            price = rows[i]['open']
            sector = sectors.get(symbol, 'UNKNOWN')  # all unknowns share the sector cap
            reason = guard.check(symbol, sector, qty, price, cash, equity, peak, positions, policy, costs)
            y = outcome(rows, i, policy, costs, max(1, qty))
            if y and y['end'] > dates[-1]:
                y = None
            d.update(reason=reason or 'PAPER_FILLED', reference_price=rows[i-1]['close'],
                     target=price*(1+policy.target), stop=price*(1-policy.stop),
                     execution_assumption='NEXT_OPEN_MARKET_PROXY_NOT_BROKER_FILL')
            forecasts.append(dict(at=at, symbol=symbol, prediction=d['prediction'], outcome=y))
            if reason:
                shadows.append(dict(**d, result=y, ledger='SHADOW_ONLY'))
                continue
            cost = costs.buy(price, qty)
            cash -= cost
            positions[symbol] = dict(at=at, qty=qty, cost=cost, sector=sector,
                                     result=y, prediction=d['prediction'])
        equity = cash + sum(v for _, v in settlement) + sum(mark(s, p) for s, p in positions.items())
        curve.append(dict(at=at, equity=equity, settled_cash=cash,
                          unsettled=sum(v for _, v in settlement), positions=len(positions)))
    return dict(identity=identity(policy, costs), policy=asdict(policy), costs=asdict(costs),
                decisions=decisions, trades=trades, shadow=shadows, equity=curve,
                open_positions=positions, forecasts=forecasts,
                metrics=metrics(trades, curve, initial, decisions))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', default='data/analysis/daily_history')
    parser.add_argument('--output', default='data/quant_experiments')
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--min-samples', type=int, default=30)
    parser.add_argument('--walk-forward', action='store_true')
    args = parser.parse_args()
    data = load_cache(args.cache)
    experiment = uuid.uuid4().hex
    dest = Path(args.output)/experiment
    dest.mkdir(parents=True, exist_ok=False)
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = 'UNKNOWN'
    manifest = dict(experiment_id=experiment, git_commit=commit, live_enabled=False,
                    implementation_sha256=hashlib.sha256(b''.join(f.read_bytes() for f in
                        sorted(Path(__file__).parent.glob('*.py')))).hexdigest(),
                    mode='HISTORICAL_REPLAY', symbols=len(data),
                    data_sha256=hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
                    limitations=['UNADJUSTED_BARS', 'NO_BOOK_SPREAD_OR_PARTIAL_FILL_DATA',
                                 'NEXT_OPEN_PROXY', 'MISSING_FUNDAMENTALS_EVENTS_AND_SECTORS',
                                 'NOT_EXISTING_BASELINE_EQUIVALENT', 'NO_FORWARD_PERFORMANCE_YET'])
    policies = [Policy('A', target=t, min_samples=args.min_samples) for t in (.015, .02)]
    policies += [Policy('B', target=.06, stop=.03, max_days=20, min_samples=args.min_samples),
                 Policy('BASELINE', target=.04, stop=.02, max_days=10)]
    summary = {}
    for policy in policies:
        name = policy.strategy+'_'+str(policy.target)
        result = replay(data, policy, start=args.start, end=args.end)
        (dest/(name+'.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        summary[name] = result['metrics']
        if args.walk_forward:
            from .validation import walk_forward
            folds = walk_forward(data, policy)
            (dest/(name+'_walk_forward.json')).write_text(json.dumps(folds, indent=2), encoding='utf-8')
    (dest/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    (dest/'comparison.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(dict(directory=str(dest), symbols=len(data), comparison=summary), ensure_ascii=True))


if __name__ == '__main__':
    main()
