"""Batch scanner with an injected authorized data provider; no API assumptions."""
from .core import features, multi_horizon, predict, validate_bars
from datetime import datetime
from market_calendar import previous_trading_day


def scan(symbols, provider, at, policy, costs, *, quantities=None, events=None,
         benchmark=None, batch_size=50):
    symbols = list(symbols)
    if not 1 <= len(symbols) <= 1000 or len(set(symbols)) != len(symbols):
        raise ValueError('UNIVERSE_MUST_BE_1_TO_1000_UNIQUE')
    if not 1 <= batch_size <= 100:
        raise ValueError('INVALID_BATCH_SIZE')
    result = []
    for offset in range(0, len(symbols), batch_size):
        batch = symbols[offset:offset+batch_size]
        try:
            data = provider(batch, at)
        except (OSError, TimeoutError, ValueError):
            data = {}
        for symbol in batch:
            try:
                rows = validate_bars(data.get(symbol, []))
            except (ValueError, KeyError, TypeError):
                result.append(dict(symbol=symbol, at=at, score=None, reason='INVALID_BARS', passed=False))
                continue
            f = features(rows, at, (events or {}).get(symbol, []), benchmark)
            row = dict(symbol=symbol, at=at, features=f, score=f['score'] if f else None,
                       reason='INSUFFICIENT_HISTORY', passed=False)
            result.append(row)
            if not f:
                continue
            if rows[-1]['source'] != 'SYNTHETIC_TEST' and f['last_bar'] != previous_trading_day(datetime.fromisoformat(at).date()).isoformat():
                row['reason'] = 'STALE_DAILY_HISTORY'
                continue
            if f['event_risk'] or f['liquidity'] < policy.min_turnover:
                row['reason'] = 'EVENT_OR_LIQUIDITY'
                continue
            signal = f['bucket'] != 'other' if policy.strategy == 'A' else f['trend'] or f['breakout']
            if not signal:
                row['reason'] = 'NO_SIGNAL'
                continue
            qty = (quantities or {}).get(symbol, 100)
            model = predict(rows, at, policy, costs, qty)
            row['prediction'] = model
            row['multi_horizon'] = multi_horizon(rows, at, policy, costs, qty)
            row['reason'] = 'INSUFFICIENT_EVIDENCE' if model['expected_net'] is None else 'NET_EDGE_UNCERTAIN'
            if model['expected_net'] is not None and model['net_lower'] > 0:
                row.update(passed=True, reason='QUANT_PASS',
                           capital_efficiency=model['expected_net']/max(1, model['expected_days']))
    return sorted(result, key=lambda r: (-r.get('capital_efficiency', -1e20), -(r['score'] or 0), r['symbol']))
