"""Adapt share-based LLM decisions to an exclusively simulated odd-lot ledger."""
from datetime import datetime
import json
from config import MAX_STRATEGY_CAPITAL_TWD, now, redact
from market import quality, execution_price
from ledger import cents, fee
import llm


def sizing(portfolio, quote):
    owned = portfolio['positions'].get(quote['symbol'], {})
    buy, sell = execution_price(quote, 'BUY'), execution_price(quote, 'SELL')
    budget = max(0, min(cents(portfolio['cash_twd'] - portfolio['reserved_capital']),
                        cents(MAX_STRATEGY_CAPITAL_TWD - portfolio['committed']),
                        cents(MAX_STRATEGY_CAPITAL_TWD * .9 - portfolio['committed']),
                        cents(MAX_STRATEGY_CAPITAL_TWD * .6) - owned.get('cost_cents', 0)))
    max_qty = 0
    if buy and buy > 0:
        for qty in range(1, 1000):
            value = cents(buy) * qty
            if value + fee(value) > budget:
                break
            max_qty = qty
    return dict(estimated_buy_price_twd=buy, estimated_sell_price_twd=sell,
                buy_budget_twd=budget / 100, max_buy_qty=max_qty,
                owned_qty=owned.get('qty', 0), max_sell_qty=min(999, owned.get('qty', 0)),
                owned_cost_twd=owned.get('cost_cents', 0) / 100,
                max_buy_total_twd=((cents(buy) * max_qty + fee(cents(buy) * max_qty)) / 100) if max_qty else 0,
                quote_basis=quote.get('quote_basis', 'capital_intraday_odd_lot'),
                note='Quantity ceilings only; freshness, observations and drawdown still apply')


def context(ledger, symbols, at, broker_inventory=None):
    portfolio = ledger.portfolio()
    symbols = list(dict.fromkeys([*symbols, *portfolio['positions']]))
    candidates = []
    for symbol in symbols:
        quote = ledger.latest(symbol, source='CAPITAL_SKCOM')
        if not quote or quote.get('source') != 'CAPITAL_SKCOM':
            continue
        quote['quality'] = quality(quote['exchange_time'], at)
        observations = ledger.history(symbol, at, source='CAPITAL_SKCOM')
        valid = [q for q in observations if q.get('source') == 'CAPITAL_SKCOM' and not q.get('is_trial')
                 and q.get('bid') and q.get('ask') and q['bid'] <= q['ask']]
        span = ((datetime.fromisoformat(valid[-1]['exchange_time']) -
                 datetime.fromisoformat(valid[0]['exchange_time'])).total_seconds()) if len(valid) > 1 else 0
        mids = [(q['bid'] + q['ask']) / 2 for q in valid]
        candidates.append(dict(symbol=symbol, quote=quote, sizing=sizing(portfolio, quote), observation_count=len(valid),
                               observation_span_seconds=span,
                               observed_return_pct=round((mids[-1] / mids[0] - 1) * 100, 4) if mids else None,
                               observed_return_basis='intraday_bid_ask_midpoint_snapshots_not_daily_bars',
                               evidence_ids=[q['evidence_id'] for q in valid],
                               observations=valid[-12:]))
    portfolio['available_cash_twd'] = max(0, portfolio['cash_twd'] - portfolio['reserved_capital'])
    portfolio['remaining_capital_twd'] = max(0, MAX_STRATEGY_CAPITAL_TWD - portfolio['committed'])
    portfolio['remaining_exposure_twd'] = max(0, MAX_STRATEGY_CAPITAL_TWD * .9 - portfolio['committed'])
    prices = {c['symbol']: (c['quote'].get('bid') or 0) for c in candidates}
    equity = portfolio['cash_twd'] + sum(
        p['qty'] * prices.get(s, 0) if prices.get(s) else p['cost_cents'] / 100
        for s, p in portfolio['positions'].items())
    # Recovered from the same ledger, including after a restart.
    peak = float(ledger.db.execute("SELECT COALESCE(MAX(CAST(detail AS REAL)),10000) FROM events WHERE kind='EQUITY_PEAK'").fetchone()[0])
    peak = max(peak, equity)
    ledger.event('EQUITY_PEAK', peak)
    return dict(time=at.isoformat(), execution_mode='SIMULATION_ONLY',
                role='Portfolio Manager', candidates=candidates, portfolio=portfolio,
                broker_inventory_observation=broker_inventory or {'status': 'NOT_QUERIED', 'positions': [],
                                                                  'usable_for_strategy_sell': False},
                evidence_ids=[c['quote']['evidence_id'] for c in candidates], memory=[],
                limits=dict(max_capital_twd=MAX_STRATEGY_CAPITAL_TWD, max_actions=3, quantity_unit='shares', min_order_qty=1, max_order_qty=999,
                            single_symbol_fraction=.6, exposure_fraction=.9,
                            min_observations=10, min_observation_span_seconds=300,
                            stop_loss_pct=3, take_profit_pct=6, max_drawdown_pct=5,
                            drawdown_pct=(peak - equity) / peak * 100,
                            fee_rate=.001425, minimum_fee_twd=20, sell_tax_rate=.003,
                            period='current_30_minute_slot', stale_quotes_cannot_fill=True))


def check(ledger, item, candidate, at, drawdown):
    if item['action'] not in {'BUY', 'SELL'}:
        return 'NO_ORDER', None
    if type(item.get('qty')) is not int or not 1 <= item['qty'] <= 999:
        return 'INVALID_ODD_LOT_QUANTITY', None
    q = candidate['quote']
    if quality(q['exchange_time'], at) != 'FRESH':
        return 'QUOTE_STALE', None
    if q.get('is_trial'):
        return 'TRIAL_QUOTE_NO_FILL', None
    if not q.get('bid') or not q.get('ask') or q['bid'] > q['ask'] or q.get('volume_shares', 0) <= 0:
        return 'QUOTE_UNUSABLE', None
    price = execution_price(q, item['action'])
    if price is None or price <= 0:
        return 'PRICE_UNAVAILABLE', None
    if item['action'] == 'BUY':
        p = ledger.portfolio()
        cost = cents(price) * item['qty']
        estimate = cost + fee(cost)
        if cents(p['committed']) + estimate > MAX_STRATEGY_CAPITAL_TWD * 100:
            return 'CAPITAL_LIMIT_REJECTED', None
        if candidate['observation_count'] < 10 or candidate['observation_span_seconds'] < 300:
            return 'INSUFFICIENT_OBSERVATIONS', None
        if drawdown >= 5:
            return 'DRAWDOWN_BUY_REJECTED', None
        owned_cost = p['positions'].get(item['symbol'], {}).get('cost_cents', 0)
        if owned_cost + estimate > MAX_STRATEGY_CAPITAL_TWD * 100 * .6:
            return 'SYMBOL_EXPOSURE_REJECTED', None
        if cents(p['committed']) + estimate > MAX_STRATEGY_CAPITAL_TWD * 100 * .9:
            return 'TOTAL_EXPOSURE_REJECTED', None
    return 'ACCEPTED', price


def run_cycle(ledger, cycle, symbols, provider=llm.decide, probe=False, stopped=lambda: False, broker_inventory=None):
    if stopped() or not ledger.claim_cycle(cycle):
        return None
    payload, raw, api = {}, '', {}
    try:
        payload = context(ledger, symbols, now(), broker_inventory)
        fresh = [c for c in payload['candidates'] if c['quote']['quality'] == 'FRESH']
        if not payload['candidates'] or (not probe and not fresh):
            ledger.save_cycle(cycle, 'SKIPPED', payload, summary='NO_FRESH_QUOTES')
            print('LLM SKIPPED: NO_FRESH_QUOTES', flush=True)
            return None
        if stopped():
            ledger.save_cycle(cycle, 'STOPPED', payload)
            return None
        # Persist the exact input before making the paid API request.
        ledger.save_cycle(cycle, 'LLM_REQUESTED', payload)
        raw, api = provider(payload)
        raw = redact(raw)
        ledger.save_cycle(cycle, 'LLM_RECEIVED', payload, raw, api)
        result = llm.validate(json.loads(raw), {c['symbol'] for c in payload['candidates']})
        if stopped():
            ledger.save_cycle(cycle, 'STOPPED', payload, raw, api, result['summary'])
            return None
        actions = result['actions'] or [dict(action='HOLD', symbol='', qty=0, reason=result['summary'])]
        lookup = {c['symbol']: c for c in payload['candidates']}
        output = []
        forced = set()
        # These fixed exit thresholds already appear in the user's prompt.md.
        if not probe:
            for symbol, p in ledger.portfolio()['positions'].items():
                c = lookup.get(symbol)
                if not c or c['quote']['quality'] != 'FRESH' or not c['quote'].get('bid'):
                    continue
                change = (c['quote']['bid'] * p['qty'] / (p['cost_cents'] / 100) - 1) * 100
                if change <= -3 or change >= 6:
                    actions.insert(0, dict(action='SELL', symbol=symbol, qty=min(p['qty'], 999),
                                           reason='STOP_LOSS_3PCT' if change <= -3 else 'TAKE_PROFIT_6PCT',
                                           source='PROMPT_RISK_RULE'))
                    forced.add(symbol)
        for item in actions:
            if stopped():
                break
            source = item.get('source', 'LLM')
            did = ledger.decision(cycle, item, raw, source)
            oid = None
            if item['action'] not in {'BUY', 'SELL'}:
                outcome = 'NO_ORDER'
            elif probe:
                outcome = 'PROBE_NO_EXECUTION'
            elif source == 'LLM' and item['symbol'] in forced:
                outcome = 'RISK_EXIT_THIS_CYCLE'
            else:
                outcome, price = check(ledger, item, lookup[item['symbol']], now(), payload['limits']['drawdown_pct'])
                if outcome == 'ACCEPTED' and not stopped():
                    outcome, oid = ledger.paper_fill(did, item['symbol'], item['action'], item['qty'], price)
                elif outcome == 'ACCEPTED':
                    outcome = 'STOPPED'
            ledger.outcome(did, outcome)
            row = dict(action=item['action'], symbol=item['symbol'], qty=item['qty'],
                       llm_wants_order=source == 'LLM' and item['action'] in {'BUY', 'SELL'},
                       simulated_order_sent=oid is not None, real_order_sent=False,
                       outcome=outcome, local_order_id=oid, reason=item['reason'], source=source)
            output.append(row)
            ledger.event('DECISION_RESULT', row)
            print('LLM DECISION: ' + json.dumps(row, ensure_ascii=False), flush=True)
        ledger.save_cycle(cycle, 'STOPPED' if stopped() else 'COMPLETED', payload, raw, api, result['summary'])
        print('LLM SUMMARY: ' + result['summary'], flush=True)
        return dict(cycle_id=cycle, summary=result['summary'], decisions=output, api=api)
    except Exception as exc:
        code = 'LLM_JSON_INVALID' if isinstance(exc, (ValueError, TypeError, KeyError)) else 'CYCLE_ERROR'
        ledger.error(code, f'{type(exc).__name__}: {redact(exc)}', cycle)
        ledger.decision(cycle, dict(action='HOLD', symbol='', qty=0, reason=code), raw, 'FALLBACK')
        ledger.save_cycle(cycle, 'ERROR_HOLD', payload, raw, api, code)
        print(f'LLM HOLD: {code} ({type(exc).__name__}); next slot continues', flush=True)
        return dict(cycle_id=cycle, error=code, decisions=[])
