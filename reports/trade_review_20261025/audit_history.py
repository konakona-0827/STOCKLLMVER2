"""Freeze sanitized local evidence and review every formal execution attempt.

Read-only SQLite transactions; no broker, model, order API, or network imports.
Replaying audit_input.json never reads or changes the live account.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
from pathlib import Path
import sqlite3

OUT = Path(__file__).resolve().parent
TAIPEI = timezone(timedelta(hours=8))
D = lambda value: Decimal(str(value))


def read_db(path, operation):
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute('BEGIN')
        return operation(con)
    finally:
        con.close()


def priced_fills(rows, quantity):
    try:
        fills = [(int(row['quantity']), D(row['price_twd'])) for row in rows or []]
        if (quantity > 0 and fills and sum(q for q, p in fills) == quantity
                and all(q > 0 and p.is_finite() and p > 0 for q, p in fills)):
            return sum((q * p for q, p in fills), D(0)) / quantity
    except (KeyError, ValueError, TypeError, ArithmeticError):
        pass
    return None


def capture(root):
    def execution(con):
        attempts = [dict(row) for row in con.execute(
            'SELECT * FROM execution_attempts ORDER BY started_at,rowid')]
        details = {row['decision_id']: json.loads(row['detail_json'] or '{}') for row in attempts}
        by_id = {row['decision_id']: row for row in attempts}
        evidence, latest_at = {}, None
        # Match exact identities before accepting an older priced broker replay.
        for snapshot in con.execute('SELECT checked_at,payload_json FROM broker_refresh_snapshots ORDER BY id DESC'):
            latest_at = latest_at or snapshot['checked_at']
            for check in json.loads(snapshot['payload_json']).get('broker_order_reconciliation', []):
                key = check.get('decision_id')
                row, detail = by_id.get(key), details.get(key, {})
                if row is None or key in evidence:
                    continue
                quantity = int(detail.get('filled_quantity') or 0)
                identity = (check.get('symbol') == row['symbol'] and check.get('side') == row['side']
                            and detail.get('seq13') and str(check.get('seq13')) == str(detail['seq13'])
                            and int(check.get('ordered_qty') or 0) == int(detail.get('quantity') or 0)
                            and int(check.get('ledger_filled_qty') or 0) == quantity)
                status_ok = (check.get('status') in ('FILL_MATCH', 'ARCHIVED_BROKER_FILL_VERIFIED')
                             and (check.get('status') == 'ARCHIVED_BROKER_FILL_VERIFIED'
                                  or check.get('broker_replay_complete') is True)
                             and int(check.get('broker_verified_filled_qty') or
                                     check.get('broker_replay_filled_qty') or 0) == quantity)
                price = priced_fills(check.get('broker_replay_fill_details'), quantity)
                if identity and status_ok and price is not None:
                    evidence[key] = dict(price=str(price), source='EXACT_BROKER_FILL_EVIDENCE',
                                         snapshot_at=snapshot['checked_at'], status=check['status'],
                                         replay_complete=check.get('broker_replay_complete'))
        safe = []
        for row in attempts:
            detail = details[row['decision_id']]
            price = evidence.get(row['decision_id'])
            safe.append({**{k: row[k] for k in ('decision_id','run_id','symbol','side','started_at','status')},
                         'quantity': detail.get('quantity'), 'filled_quantity': int(detail.get('filled_quantity') or 0),
                         'limit_price': detail.get('limit_price'), 'broker_order_sent': detail.get('broker_order_sent'),
                         'execution_reason': detail.get('reason'), 'price_evidence': price,
                         'fee_reserve_twd': detail.get('fee_reserve_twd'),
                         'fill_confirmation_source': detail.get('fill_confirmation_source')})
        return dict(attempts=safe, latest_broker_snapshot_at=latest_at,
                    positions=[dict(row) for row in con.execute('SELECT symbol,qty FROM ai_positions')],
                    adjustment_count=con.execute('SELECT COUNT(*) FROM ai_position_adjustments').fetchone()[0],
                    adoptions=[dict(row) for row in con.execute(
                        'SELECT symbol,qty,unit_cost_twd,adopted_at FROM manual_ai_adoptions')])

    executed = read_db(root/'data/execution/live_execution.sqlite3', execution)
    order_symbols = defaultdict(set)
    for row in executed['attempts']:
        order_symbols[row['run_id']].add(row['symbol'])

    def analysis(con):
        scans, contexts = [], {}
        for row in con.execute('SELECT * FROM scan_runs LEFT JOIN selection_analyses USING(run_id) ORDER BY run_id'):
            manifest = json.loads(row['manifest_json'])
            decision = json.loads(row['decision_json'] or '{}')
            strategy = manifest.get('strategy') or {}
            scans.append(dict(run_id=row['run_id'], started_at=manifest.get('started_at'),
                status=manifest.get('status'), validation=manifest.get('ai_validation'),
                strategy_name=strategy.get('name'), strategy_version=strategy.get('version'),
                model=manifest.get('llm_model'),
                actions=dict(Counter(d['decision'] for d in decision.get('decisions', [])))))
            if row['run_id'] not in order_symbols:
                continue
            request = json.loads(row['request_json'] or '{}')
            payload = json.loads(request.get('input') or '{}')
            symbols = order_symbols[row['run_id']]
            candidates = [{k: c.get(k) for k in (
                'symbol','name','asset_type','last_price','bid','ask','open','high','low',
                'quote_status','quote_timestamp','is_trial','daily_bars','daily_trend',
                'daily_history_status','max_sell_qty','ai_trade_dates','ai_cost_basis','eligible_strategies')}
                for c in payload.get('analysis_set', []) if c['symbol'] in symbols]
            contexts[row['run_id']] = dict(candidates=candidates,
                decisions=[d for d in decision.get('decisions', []) if d['symbol'] in symbols],
                available_buy_budget_twd=payload.get('available_buy_budget_twd'),
                cost_rules=payload.get('paper_simulation_rules'))
        marks = {}
        for row in con.execute('SELECT run_id,symbol,exchange_time,quote_status,snapshot_json '
                               'FROM multi_market_snapshots ORDER BY run_id'):
            quote = json.loads(row['snapshot_json'])
            if quote.get('bid') is not None:
                marks[row['symbol']] = dict(run_id=row['run_id'], exchange_time=row['exchange_time'],
                    quote_status=row['quote_status'], bid=quote['bid'], last_price=quote.get('last_price'))
        return dict(scans=scans, contexts=contexts, latest_stored_marks=marks)

    return dict(captured_at=datetime.now(TAIPEI).isoformat(),
                source='latest local STOCKLLMVER2 runtime; two read-only database transactions',
                execution=executed, analysis=read_db(root/'data/analysis/market_history.sqlite3', analysis))


def fee(gross, minimum):
    return max(D(minimum), (gross*D('0.001425')).to_integral_value(rounding=ROUND_FLOOR))


def tax(gross, symbol):
    return (gross*D('0.001' if symbol == '0050' else '0.003')).to_integral_value(rounding=ROUND_CEILING)


def economics(candidate, decision, minimum):
    try:
        q = int(decision['suggested_qty'])
        entry = max(D(candidate['ask']), D(decision['suggested_price']))
        spread = D(candidate['ask'])-D(candidate['bid'])
        target, stop = D(decision['target_price'])-spread, D(decision['invalidation_price'])-spread
        if not 0 < stop < entry < target or q <= 0:
            return None
        upside = (target-entry)*q-fee(entry*q, minimum)-fee(target*q, minimum)-tax(target*q,candidate['symbol'])
        downside = (entry-stop)*q+fee(entry*q, minimum)+fee(stop*q, minimum)+tax(stop*q,candidate['symbol'])
        return dict(net_upside=str(upside), net_downside=str(downside),
                    net_return=str(upside/(entry*q)), reward_risk=str(upside/downside))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def analyze(data):
    if data['execution'].get('adjustment_count', 0):
        raise ValueError('Extend lot accounting explicitly before reviewing manually resized positions')
    scans = {r['run_id']: r for r in data['analysis']['scans']}
    contexts = data['analysis']['contexts']
    lots, pairs, reviews = defaultdict(list), [], []
    for number, row in enumerate(data['execution']['attempts'], 1):
        context = contexts.get(row['run_id'], {})
        decision = next((d for d in context.get('decisions', []) if d['symbol']==row['symbol']), {})
        candidate = next((c for c in context.get('candidates', []) if c['symbol']==row['symbol']), {})
        verified = row.get('price_evidence')
        price = D(verified['price']) if verified else None
        filled = row['filled_quantity']
        entry_qty = int(decision.get('suggested_qty') or 0)
        budget = context.get('available_buy_budget_twd')
        notional = (D(decision['suggested_price'])*entry_qty
                    if row['side']=='BUY' and decision.get('suggested_price') else None)
        review = dict(number=number, **row, strategy=scans.get(row['run_id'], {}),
                      decision=decision, candidate=candidate, available_budget=budget,
                      notional_budget_fraction=str(notional/D(budget)) if notional is not None and budget else None,
                      plan_scenarios={str(m): economics(candidate,decision,m) for m in (1,20)}
                      if row['side']=='BUY' else {})
        reviews.append(review)
        if filled <= 0:
            continue
        if row['side']=='BUY':
            lots[row['symbol']].append(dict(row=row, remaining=filled, price=price))
        else:
            remaining, matched, known, buy_gross, scenario_buy = filled, [], price is not None, D(0), D(0)
            buy_fees = {m: D(0) for m in (1,20)}
            while remaining and lots[row['symbol']]:
                lot = lots[row['symbol']][0]
                count = min(remaining, lot['remaining'])
                buy = lot['row']
                actual = lot['price']
                known = known and actual is not None
                assumed = actual if actual is not None else D(buy['limit_price'])
                scenario_buy += assumed*count
                if actual is not None:
                    buy_gross += actual*count
                for m in buy_fees:
                    buy_fees[m] += fee(assumed*buy['filled_quantity'],m)*count/buy['filled_quantity']
                matched.append(dict(buy_number=next(r['number'] for r in reviews if r['decision_id']==buy['decision_id']), qty=count))
                remaining -= count
                lot['remaining'] -= count
                if not lot['remaining']:
                    lots[row['symbol']].pop(0)
            if remaining:
                raise ValueError('Unmatched SELL: do not infer an AI entry or fabricate a cost basis')
            assumed_exit = price if price is not None else D(row['limit_price'])
            pair = dict(symbol=row['symbol'], sell_number=number, qty=filled, matched=matched,
                        prices_verified=known, gross_pnl=str(price*filled-buy_gross) if known else None,
                        scenarios={str(m): str(assumed_exit*filled-scenario_buy-buy_fees[m]
                            -fee(assumed_exit*filled,m)-tax(assumed_exit*filled,row['symbol'])) for m in (1,20)},
                        scenario_basis='VERIFIED_PRICES_WITH_ASSUMED_COSTS' if known else 'MISSING_PRICE_AT_ORDER_LIMIT_ILLUSTRATION_ONLY')
            pairs.append(pair)
    expected = defaultdict(int)
    for symbol, rows in lots.items():
        expected[symbol] += sum(row['remaining'] for row in rows)
    for adoption in data['execution']['adoptions']:
        expected[adoption['symbol']] += int(adoption['qty'])
    recorded = {p['symbol']:p['qty'] for p in data['execution']['positions'] if p['qty']}
    assert {s:q for s,q in expected.items() if q} == recorded, 'Historical fills do not reconcile to current AI quantity'
    verified_pairs = [pair for pair in pairs if pair['prices_verified']]
    return dict(attempt_count=len(reviews), filled_order_count=sum(r['filled_quantity']>0 for r in reviews),
                status_counts=dict(Counter(r['status'] for r in reviews)), closed_cycles=pairs,
                verified_cycle_count=len(verified_pairs), verified_gross_total=str(sum((D(p['gross_pnl']) for p in verified_pairs),D(0))),
                verified_cost_scenario_totals={str(m):str(sum((D(p['scenarios'][str(m)]) for p in verified_pairs),D(0))) for m in (1,20)},
                scan_count=len(scans), scans_by_strategy=dict(Counter(r.get('strategy_name') or 'UNVERSIONED' for r in scans.values())),
                all_trades_model_counts=dict(Counter(r['strategy'].get('model') for r in reviews)),
                open_ai_quantities=recorded, positions_reconciled=True, reviews=reviews)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path)
    args = parser.parse_args()
    snapshot = OUT/'audit_input.json'
    if not snapshot.exists():
        if args.source_root is None:
            parser.error('--source-root is required only for the first capture')
        snapshot.write_text(json.dumps(capture(args.source_root.resolve()),ensure_ascii=False,indent=2),encoding='utf-8')
    data = json.loads(snapshot.read_text(encoding='utf-8'))
    result = analyze(data)
    result['input_sha256'] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    (OUT/'audit_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='reviews'},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
