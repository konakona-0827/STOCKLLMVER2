"""Run one explicit forward paper cycle from a saved verified quote bundle.

Bundle: {at, quotes: [{symbol,bid,ask,bid_size,ask_size,quote_status,
quote_timestamp,last_price,lot_type}], sessions: [ISO dates], sectors: {},
events: {symbol: [{kind,published_at,source,score,risk}]}}.
No scheduler or live-order configuration is modified.
"""
import argparse
import json
from datetime import datetime
from pathlib import Path
from .core import Costs, Policy
from .forward import ForwardPaper, exit_signal
from .runner import load_cache
from .scanner import scan


def cycle(path, data, bundle, policy, costs=Costs()):
    paper = ForwardPaper(path, policy, costs)
    at, quotes = bundle['at'], {q['symbol']: q for q in bundle['quotes']}
    session = at[:10]
    with paper.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS cycles(at TEXT PRIMARY KEY, payload TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS shadow(id TEXT PRIMARY KEY, payload TEXT)')
        previous = db.execute('SELECT payload FROM cycles WHERE at=?', (at,)).fetchone()
        if previous:
            return json.loads(previous[0])
        holdings = {s: json.loads(p) for s, p in db.execute('SELECT symbol,payload FROM positions')}
        shadow_positions = {s: json.loads(p) for s, p in db.execute('SELECT id,payload FROM shadow')}
    results = []
    sessions = sorted(set(bundle.get('sessions', [])))
    def fresh(q):
        try:
            age = (datetime.fromisoformat(at)-datetime.fromisoformat(q['quote_timestamp'])).total_seconds()
            return (0 <= age <= 120 and q.get('quote_status') == 'LIVE' and not q.get('is_trial')
                    and q.get('lot_type') == 'INTRADAY_ODD_LOT' and q.get('book_size_unit') == 'shares')
        except (ValueError, KeyError, TypeError):
            return False
    marks = {s: q['bid'] for s, q in quotes.items() if fresh(q) and q.get('bid') is not None}
    for sid, p in shadow_positions.items():
        if p.get('closed_at') or session <= p['at'][:10]:
            continue
        quote = quotes.get(p['symbol'], {})
        # Shadow results also require current, executable, nontrial odd-lot data.
        try:
            age = (datetime.fromisoformat(at)-datetime.fromisoformat(quote['quote_timestamp'])).total_seconds()
        except (KeyError, ValueError, TypeError):
            continue
        if not fresh(quote):
            continue
        days = len([d for d in sessions if p['at'][:10] < d <= session])
        signal, stop = exit_signal(p, quote, days, policy)
        p['stop'] = stop
        if signal.startswith('SELL') and quote.get('bid_size', 0) >= p['qty']:
            p.update(closed_at=at, exit_reason=signal, exit_price=quote['bid'],
                     hypothetical_net=costs.pnl(p['entry'], quote['bid'], p['qty']))
        with paper.connect() as db:
            db.execute('UPDATE shadow SET payload=? WHERE id=?', (json.dumps(p), sid))
    for symbol, p in holdings.items():
        q = quotes.get(symbol, {})
        days = len([d for d in sessions if p['at'][:10] < d <= session])
        signal, stop = exit_signal(p, q, days, policy,
                                  [b['close'] for b in data.get(symbol, []) if b['date'] < session])
        if signal.startswith('SELL') and q:
            future = [d for d in sessions if d > session]
            results.append(paper.execute(at+':SELL:'+symbol, at, symbol, 'SELL', p['qty'], q,
                reason=signal, sessions=sessions, settlement_date=future[1] if len(future)>1 else None))
        elif signal.startswith('HOLD'):
            p.update(stop=stop, review_at=at, review_reason=signal, next_review='NEXT_SESSION_OR_EVENT')
            with paper.connect() as db:
                db.execute('UPDATE positions SET payload=? WHERE symbol=?', (json.dumps(p), symbol))
    # Use the current portfolio budget and conservative share sizing before the
    # statistically calibrated cash/risk check in ForwardPaper.execute.
    with paper.connect() as db:
        capital = db.execute('SELECT cash FROM state').fetchone()[0]
    quantities = {s: max(1, min(999, int(capital*policy.position_fraction/q['ask'])-1))
                  for s, q in quotes.items() if type(q.get('ask')) in (float, int) and q['ask'] > 0}
    candidates = scan(list(data), lambda syms, _: {s: data[s] for s in syms}, session,
                      policy, costs, quantities=quantities, events=bundle.get('events'),
                      benchmark=bundle.get('benchmark'))
    shadow = []
    for c in candidates:
        if not c['passed']:
            continue
        symbol = c['symbol']
        if symbol not in quotes:
            shadow.append(dict(candidate=c, reason='NO_EXECUTABLE_QUOTE'))
            continue
        r = paper.execute(at+':BUY:'+symbol, at, symbol, 'BUY', quantities.get(symbol, 0),
                          quotes[symbol], reason='QUANT_PASS', portfolio_marks=marks,
                          sector=bundle.get('sectors', {}).get(symbol, 'UNKNOWN'))
        results.append(r)
        if r['filled_qty'] < r['requested_qty']:
            shadow.append(dict(candidate=c, reason=r['reason'] if r['filled_qty'] == 0 else 'UNFILLED_REMAINDER',
                               unfilled_qty=r['requested_qty']-r['filled_qty']))
    for item in shadow:
        c = item['candidate']
        q = quotes.get(c['symbol'], {})
        if not q.get('ask') or not fresh(q):
            continue
        qty = item.get('unfilled_qty', quantities.get(c['symbol'], 0))
        if qty <= 0:
            continue
        payload = dict(symbol=c['symbol'], at=at, entry=q['ask'], qty=qty,
                       stop=q['ask']*(1-policy.stop), target=q['ask']*(1+policy.target),
                       reason=item['reason'], prediction=c['prediction'],
                       ledger='HYPOTHETICAL_SHADOW_NOT_REAL_FILL')
        with paper.connect() as db:
            db.execute('INSERT OR IGNORE INTO shadow VALUES(?,?)',
                       (at+':'+c['symbol'], json.dumps(payload)))
    report = dict(at=at, strategy=policy.strategy, candidates=candidates, execution=results,
                  shadow=shadow, shadow_ledger='SEPARATE_FROM_FORWARD_FILLS', live_enabled=False)
    with paper.connect() as db:
        db.execute('INSERT OR IGNORE INTO cycles VALUES(?,?)', (at, json.dumps(report)))
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle', required=True)
    p.add_argument('--cache', default='data/analysis/daily_history')
    p.add_argument('--output', required=True, help='New isolated experiment directory')
    a = p.parse_args()
    bundle = json.loads(Path(a.bundle).read_text(encoding='utf-8-sig'))
    data = load_cache(a.cache)
    dest = Path(a.output)
    dest.mkdir(parents=True, exist_ok=True)
    for policy in (Policy('A'), Policy('B', target=.06, stop=.03, max_days=20)):
        result = cycle(dest/(policy.strategy+'.sqlite3'), data, bundle, policy)
        print(json.dumps(dict(strategy=policy.strategy, scanned=len(result['candidates']),
                              executions=result['execution']), ensure_ascii=True))
