"""A/B event-driven price plans and resting PAPER orders.

python -m quant_research.planned_cycle --bundle quotes.json --output NEW_DIRECTORY
Uses the same saved verified quote bundle as quant_research.cycle. Never calls
the live execution dispatcher; one ledger per strategy, no LLM market polling.
"""
from dataclasses import asdict
from pathlib import Path
from decimal import Decimal
import argparse
import json
import hashlib

from .core import Costs, Policy
from .forward import exit_signal
from .limit_orders import LimitOrderBook, PricePlan, floor_stock_price, aware, ACTIVE
from .runner import load_cache
from .scanner import scan


def willing_entry(candidate, rows, policy, at, qty):
    """Explicit reference policy: shallow pullback, bounded by ATR and MA5.

    No fabricated probability: the caller must already have a quant pass. The
    formula is versioned and testable, not a promise of superior performance.
    """
    if not candidate.get('passed'):
        raise ValueError('QUANT_PASS_REQUIRED')
    f = candidate['features']
    previous = [b for b in rows if b['date'] < aware(at).date().isoformat()]
    reference = Decimal(str(previous[-1]['close']))
    ma5 = Decimal(str(f['ma']['5']))
    pullback = min(Decimal('.005'), Decimal(str(f['atr']))*Decimal('.25'))
    price = floor_stock_price(min(reference, ma5)*(1-pullback))
    expiry = aware(at).replace(hour=13, minute=30, second=0, microsecond=0).isoformat()
    return PricePlan(plan_id=f"{policy.strategy}:{candidate['symbol']}:{aware(at).isoformat()}:ENTRY",
        symbol=candidate['symbol'], side='BUY', qty=qty, limit_price=str(price),
        target_price=str(price*(1+Decimal(str(policy.target)))),
        stop_price=str(price*(1-Decimal(str(policy.stop)))), created_at=at, expires_at=expiry,
        rationale='QUANT_PASS; min(previous_close, MA5) less min(0.5%, 0.25 ATR)',
        strategy_version='planned-entry-v1', model_version=candidate['prediction']['model_version'])


def cycle(path, data, bundle, policy, costs=Costs()):
    book = LimitOrderBook(path, policy, costs)
    at, stamp = bundle['at'], aware(bundle['at'])
    quotes = {q['symbol']: q for q in bundle['quotes']}
    tick_id = bundle.get('tick_id', at)
    input_sha256 = hashlib.sha256(json.dumps(bundle, sort_keys=True).encode()).hexdigest()
    with book.paper.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS planned_cycles(id TEXT PRIMARY KEY, payload TEXT)')
        old = db.execute('SELECT payload FROM planned_cycles WHERE id=?', (tick_id,)).fetchone()
        if old:
            result = json.loads(old[0])
            if result.get('input_sha256') != input_sha256:
                raise ValueError('CYCLE_ID_INPUT_MISMATCH')
            return result
    # Process the already resting orders before generating new plans, so a new
    # plan cannot be filled retrospectively using the creation snapshot.
    invalidated = set(bundle.get('invalidated_symbols', []))
    for symbol, events in bundle.get('events', {}).items():
        if any(e.get('source') and e.get('risk') == 'BLOCK' and
               e.get('published_at', '9999') <= at for e in events):
            invalidated.add(symbol)
    fills = book.process(tick_id, at, quotes, sessions=bundle.get('sessions', []),
                         invalidated_symbols=invalidated)
    snapshot = book.snapshot()
    # Cancellation acknowledgement is its own simulated event, after processing
    # possible in-flight fills; real broker cancellation is never inferred here.
    for order in snapshot['orders']:
        if order['status'] == 'CANCEL_PENDING':
            book.confirm_cancel(order['plan_id'], at)
    snapshot = book.snapshot()
    plans, rejected = [], []
    active_symbols = {o['symbol'] for o in snapshot['orders'] if o['status'] in ACTIVE}
    if stamp.strftime('%H:%M:%S') >= '13:30:00':
        candidates = []
    else:
        quantities = {s: min(999, max(1, int(snapshot['available']*policy.position_fraction/q['ask'])-1))
                      for s, q in quotes.items() if q.get('ask') and q['ask'] > 0}
        candidates = scan(list(data), lambda symbols, _: {s: data[s] for s in symbols},
                          stamp.date().isoformat(), policy, costs, quantities=quantities,
                          events=bundle.get('events'), benchmark=bundle.get('benchmark')) if data else []
        for c in candidates:
            symbol = c['symbol']
            if not c.get('passed') or symbol in snapshot['positions'] or symbol in active_symbols:
                continue
            try:
                if symbol in invalidated:
                    raise ValueError('EVENT_INVALIDATED')
                plan = willing_entry(c, data[symbol], policy, at, quantities.get(symbol, 0))
                book.create(plan)
                book.submit(plan.plan_id, at, quotes)
                plans.append(asdict(plan))
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                rejected.append(dict(symbol=symbol, reason=str(exc)))
    # Exits also rest as limit orders; a sell is never fabricated on a target
    # touch. Stop exits remain subject to limit-order nonexecution risk.
    for symbol, p in snapshot['positions'].items():
        if symbol in active_symbols or symbol not in quotes:
            continue
        if aware(p['at']).date() >= stamp.date():
            continue  # no repeated same-day odd-lot exit proposals
        days = len([s for s in bundle.get('sessions', []) if p['at'][:10] < s <= stamp.date().isoformat()])
        signal, stop = exit_signal(p, quotes[symbol], days, policy,
                                  [b['close'] for b in data.get(symbol, []) if b['date'] < stamp.date().isoformat()])
        if symbol in invalidated:
            signal = 'SELL_EVENT_INVALIDATED'
        if signal.startswith('SELL') and stamp.strftime('%H:%M:%S') < '13:30:00':
            try:
                price = floor_stock_price(quotes[symbol]['bid'])
                plan = PricePlan(f'{policy.strategy}:{symbol}:{stamp.isoformat()}:EXIT', symbol,
                    'SELL', p['qty'], str(price), str(p['target']), str(stop), at,
                    stamp.replace(hour=13, minute=30, second=0, microsecond=0).isoformat(),
                    signal, 'planned-exit-v1', 'deterministic-exit-v1')
                book.create(plan)
                book.submit(plan.plan_id, at, quotes)
                plans.append(asdict(plan))
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                rejected.append(dict(symbol=symbol, reason=str(exc)))
        elif signal.startswith('HOLD'):
            p.update(stop=stop, review_at=at, review_reason=signal)
            with book.paper.connect() as db:
                db.execute('UPDATE positions SET payload=? WHERE symbol=?', (json.dumps(p), symbol))
    result = dict(at=at, tick_id=tick_id, input_sha256=input_sha256, strategy=policy.strategy, live_enabled=False,
                  plans=plans, fills=fills, candidates=candidates, rejected=rejected,
                  account=book.snapshot(), simulation='OBSERVED_BOOK_PROXY_NOT_GUARANTEED_FILL')
    with book.paper.connect() as db:
        db.execute('INSERT OR IGNORE INTO planned_cycles VALUES(?,?)', (tick_id, json.dumps(result)))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle', required=True)
    p.add_argument('--cache', default='data/analysis/daily_history')
    p.add_argument('--output', required=True)
    a = p.parse_args()
    data = load_cache(a.cache)
    bundle = json.loads(Path(a.bundle).read_text(encoding='utf-8-sig'))
    for policy in (Policy('A'), Policy('B', target=.06, stop=.03, max_days=20)):
        result = cycle(Path(a.output)/(policy.strategy+'.sqlite3'), data, bundle, policy)
        print(json.dumps(dict(strategy=policy.strategy, plans=len(result['plans']),
                             fills=len(result['fills']), reserved=result['account']['reserved']), ensure_ascii=True))


if __name__ == '__main__':
    main()
