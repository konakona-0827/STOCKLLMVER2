"""Read-only research audit. No broker, LLM, network, or application imports.

First run captures selected local evidence; later runs replay the frozen capture.
Writes only audit_input.json and audit_results.json alongside this script.
The capture excludes account numbers, broker order IDs and account balances.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from pathlib import Path
import hashlib
import json
import sqlite3

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
TAIPEI = timezone(timedelta(hours=8))


def read_db(relative, callback):
    con = sqlite3.connect((ROOT / relative).as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute('BEGIN')
        return callback(con)
    finally:
        con.close()


def capture():
    def execution(con):
        row = con.execute('SELECT id,checked_at,payload_json FROM broker_refresh_snapshots '
                          'ORDER BY id DESC LIMIT 1').fetchone()
        payload = json.loads(row['payload_json'])
        checks = {}
        for item in payload.get('broker_order_reconciliation', []):
            checks[item['decision_id']] = {key: item.get(key) for key in (
                'broker_replay_fill_details', 'broker_verified_filled_qty',
                'user_confirmed_filled_qty', 'status', 'archived_broker_fill_checked_at')}
        attempts = []
        for row_a in con.execute('SELECT * FROM execution_attempts ORDER BY started_at,rowid'):
            a = dict(row_a)
            detail = json.loads(a.pop('detail_json') or '{}')
            a['detail'] = {key: detail.get(key) for key in (
                'quantity', 'filled_quantity', 'limit_price', 'broker_order_sent',
                'broker_replay_fill_details', 'fill_confirmation_source', 'reason')}
            a['price_check'] = checks.get(a['decision_id'], {})
            attempts.append(a)
        return dict(attempts=attempts, snapshot_id=row['id'], snapshot_at=row['checked_at'],
                    positions=[dict(r) for r in con.execute('SELECT * FROM ai_positions')],
                    adoptions=[dict(r) for r in con.execute('SELECT * FROM manual_ai_adoptions')],
                    adjustments=[dict(r) for r in con.execute('SELECT * FROM ai_position_adjustments')])

    def analysis(con):
        runs = []
        for row in con.execute('SELECT * FROM scan_runs JOIN selection_analyses USING(run_id) '
                               'ORDER BY run_id'):
            manifest = json.loads(row['manifest_json'])
            req = json.loads(row['request_json'] or '{}')
            inp = json.loads(req.get('input') or '{}')
            resp = json.loads(row['response_json'] or '{}')
            candidates = []
            for candidate in inp.get('analysis_set', []):
                candidates.append({key: candidate.get(key) for key in (
                    'symbol', 'name', 'asset_type', 'last_price', 'open', 'high', 'low',
                    'bid', 'ask', 'quote_timestamp', 'quote_status', 'is_trial',
                    'market_history', 'daily_history_status', 'daily_trend',
                    'ai_cost_basis', 'ai_trade_dates', 'max_sell_qty', 'paper_position',
                    'eligible_strategies')})
            runs.append(dict(run_id=row['run_id'], started_at=manifest['started_at'],
                             validation=manifest.get('ai_validation'), status=manifest.get('status'),
                             strategy_version=manifest.get('strategy', {}).get('version', 'unknown'),
                             strategy_name=manifest.get('strategy', {}).get('name', 'unknown'),
                             model=manifest.get('llm_model'), candidates=candidates,
                             rules=inp.get('paper_simulation_rules'),
                             usage=(resp.get('api') or {}).get('usage') or {},
                             decision=json.loads(row['decision_json'] or '{}')))
        return runs

    return dict(captured_at=datetime.now(TAIPEI).isoformat(),
                execution=read_db('data/execution/live_execution.sqlite3', execution),
                runs=read_db('data/analysis/market_history.sqlite3', analysis))


def verified_price(a):
    qty = int(a['detail'].get('filled_quantity') or 0)
    for source in (a['price_check'], a['detail']):
        fills = source.get('broker_replay_fill_details') or []
        if fills and sum(int(f['quantity']) for f in fills) == qty and qty > 0:
            prices = [Decimal(str(f['price_twd'])) for f in fills]
            if all(p.is_finite() and p > 0 for p in prices):
                return sum(p * int(f['quantity']) for p, f in zip(prices, fills)) / qty
    return None


def fee(gross, minimum):
    return max(Decimal(minimum), (gross * Decimal('0.001425')).to_integral_value(rounding=ROUND_FLOOR))


def tax(gross, symbol):
    return (gross * Decimal('0.001' if symbol == '0050' else '0.003')).to_integral_value(rounding=ROUND_CEILING)


def costs(buy, sell, qty, symbol, minimum):
    gross_buy, gross_sell = buy * qty, sell * qty
    fees = fee(gross_buy, minimum) + fee(gross_sell, minimum)
    sale_tax = tax(gross_sell, symbol)
    return dict(gross_pnl=float(gross_sell-gross_buy), commissions=float(fees),
                sell_tax=float(sale_tax), net_pnl=float(gross_sell-gross_buy-fees-sale_tax))


def analyze(data):
    runs = {r['run_id']: r for r in data['runs']}
    versions = {}
    input_tokens = output_tokens = usage_runs = 0
    for r in data['runs']:
        v = versions.setdefault(r['strategy_version'], dict(runs=0, valid=0, first=r['run_id'],
                                                           last=r['run_id'], actions=Counter()))
        v['runs'] += 1
        v['valid'] += r['validation'] == 'VALID'
        v['last'] = r['run_id']
        v['actions'].update(d['decision'] for d in r['decision'].get('decisions', []))
        usage_runs += bool(r['usage'])
        input_tokens += int(r['usage'].get('input_tokens') or 0)
        output_tokens += int(r['usage'].get('output_tokens') or 0)

    inventory = defaultdict(list)
    trades, closed = [], []
    for a in data['execution']['attempts']:
        qty = int(a['detail'].get('filled_quantity') or 0)
        if qty <= 0:
            continue
        run = runs[a['run_id']]
        cand = next((c for c in run['candidates'] if c['symbol'] == a['symbol']), {})
        decision = next((d for d in run['decision'].get('decisions', []) if d['symbol'] == a['symbol']), {})
        price = verified_price(a)
        trade = dict(symbol=a['symbol'], side=a['side'], qty=qty, run_id=a['run_id'],
                     decision_id=a['decision_id'],
                     submission_time_taipei=datetime.fromisoformat(a['started_at']).astimezone(TAIPEI).isoformat(),
                     verified_price=float(price) if price is not None else None,
                     limit_price=a['detail'].get('limit_price'), price_status=a['price_check'].get('status'),
                     strategy_version=run['strategy_version'], decision=decision, candidate=cand,
                     input_cost_rules=run['rules'])
        trades.append(trade)
        if a['side'] == 'BUY':
            inventory[a['symbol']].append(trade)
        elif a['side'] == 'SELL':
            # Deliberately require an exact single-lot match for this audit;
            # manual transfers and ambiguous matches must not become AI entry performance.
            lots = inventory[a['symbol']]
            if len(lots) != 1 or lots[0]['qty'] != qty:
                raise ValueError('Ambiguous lot match; extend the auditor explicitly before reuse')
            buy = lots.pop()
            known = buy['verified_price'] is not None and trade['verified_price'] is not None
            pair = dict(symbol=a['symbol'], qty=qty, buy_run=buy['run_id'], sell_run=a['run_id'],
                        buy_price=buy['verified_price'], sell_price=trade['verified_price'],
                        price_complete=known, entry_version=buy['strategy_version'],
                        exit_version=trade['strategy_version'])
            # Limit prices are only used in a separately labelled sensitivity illustration.
            bp = Decimal(str(buy['verified_price'] if buy['verified_price'] is not None else buy['limit_price']))
            sp = Decimal(str(trade['verified_price'] if trade['verified_price'] is not None else trade['limit_price']))
            pair['scenario_prices'] = dict(buy=float(bp), sell=float(sp),
                                          basis='VERIFIED' if known else 'UNVERIFIED_LEG_AT_ORDER_LIMIT_NOT_ACTUAL_PNL')
            pair['cost_scenarios'] = {str(f): costs(bp, sp, qty, a['symbol'], f) for f in (1, 20)}
            closed.append(pair)

    plans = []
    for trade in trades:
        d, c = trade['decision'], trade['candidate']
        if trade['side'] != 'BUY' or not d.get('target_price'):
            continue
        # Match the proposed size/price used at decision time, not hindsight execution.
        qty = int(d['suggested_qty'])
        entry = max(Decimal(str(c['ask'])), Decimal(str(d.get('suggested_price') or c['ask'])))
        spread = Decimal(str(c['ask']))-Decimal(str(c['bid']))
        target = Decimal(str(d['target_price']))-spread
        stop = Decimal(str(d['invalidation_price']))-spread
        item = dict(symbol=trade['symbol'], run_id=trade['run_id'], qty=qty, scenarios={})
        for minimum in (1, 20):
            upside = costs(entry, target, qty, trade['symbol'], minimum)['net_pnl']
            downside = -costs(entry, stop, qty, trade['symbol'], minimum)['net_pnl']
            item['scenarios'][str(minimum)] = dict(net_upside=upside, net_downside=downside,
                                                 reward_risk=upside/downside)
        plans.append(item)
    verified_pairs = [p for p in closed if p['price_complete']]
    return dict(captured_at=data['captured_at'], broker_snapshot_at=data['execution']['snapshot_at'],
                scan_count=len(runs), first_run=min(runs), last_run=max(runs),
                scans_by_day=dict(Counter(r['run_id'][:8] for r in data['runs'])),
                validation_counts=dict(Counter(r['validation'] for r in data['runs'])),
                versions=versions, usage=dict(runs_with_usage=usage_runs, input_tokens=input_tokens,
                                            output_tokens=output_tokens, actual_cost='UNKNOWN'),
                attempt_count=len(data['execution']['attempts']),
                attempt_status_counts=dict(Counter(a['status'] for a in data['execution']['attempts'])),
                submitted_attempts=sum(bool(a['detail'].get('broker_order_sent')) for a in data['execution']['attempts']),
                filled_orders=len(trades), closed_pairs=closed,
                verified_price_pair_count=len(verified_pairs),
                verified_price_subset_totals={str(f): sum(p['cost_scenarios'][str(f)]['net_pnl'] for p in verified_pairs) for f in (1, 20)},
                all_pairs_limit_price_illustration_totals={str(f): sum(p['cost_scenarios'][str(f)]['net_pnl'] for p in closed) for f in (1, 20)},
                plans=plans, trade_evidence=trades, adoptions=data['execution']['adoptions'],
                positions=data['execution']['positions'],
                limitations=['Fees/taxes are scenarios, not broker settlement amounts.',
                             'Submission date is not an exact broker fill timestamp.',
                             'No account return, Sharpe, drawdown or validated win rate is inferred.',
                             'Sample contains multiple strategy versions; manual entry is excluded.',
                             'Two databases captured separately; capture times are recorded.'])


if __name__ == '__main__':
    snapshot = OUT / 'audit_input.json'
    if not snapshot.exists():
        snapshot.write_text(json.dumps(capture(), ensure_ascii=False, indent=2), encoding='utf-8')
    data = json.loads(snapshot.read_text(encoding='utf-8'))
    result = analyze(data)
    result['input_sha256'] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    (OUT / 'audit_results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k not in ['trade_evidence', 'positions', 'versions', 'adoptions', 'limitations']},
                     ensure_ascii=False, indent=2))
