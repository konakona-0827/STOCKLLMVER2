"""Adapt share-based LLM decisions to an exclusively simulated odd-lot ledger."""
from datetime import datetime
from email.utils import parsedate_to_datetime
import json
import math
import statistics
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
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


def market_analysis(observations, mids):
    """Summarize observed intraday quote snapshots; these are not daily bars."""
    changes = [(right / left - 1) * 100 for left, right in zip(mids, mids[1:]) if left]
    latest = mids[-1] if mids else None
    features = {
        'observed_return_pct': round((mids[-1] / mids[0] - 1) * 100, 4) if len(mids) > 1 and mids[0] else None,
        'sample_return_pct': {},
        'distance_from_average_pct': {},
        'realized_volatility_pct': round(statistics.pstdev(changes[-20:]), 4) if len(changes) >= 5 else None,
        'return_autocorrelation_lag1': None,
        'recent_direction_counts': {},
        'observed_midpoint_range': None,
        'current_range_position_pct': None,
    }
    for window in (3, 5, 10, 20):
        features['sample_return_pct'][str(window)] = (
            round((mids[-1] / mids[-window - 1] - 1) * 100, 4)
            if len(mids) > window and mids[-window - 1] else None)
        average = statistics.fmean(mids[-window:]) if len(mids) >= window else None
        features['distance_from_average_pct'][str(window)] = (
            round((latest / average - 1) * 100, 4) if latest and average else None)
    if len(changes) >= 5:
        left, right = changes[:-1], changes[1:]
        left_mean, right_mean = statistics.fmean(left), statistics.fmean(right)
        numerator = sum((a-left_mean)*(b-right_mean) for a,b in zip(left,right))
        denominator = math.sqrt(sum((a-left_mean)**2 for a in left) * sum((b-right_mean)**2 for b in right))
        features['return_autocorrelation_lag1'] = round(numerator / denominator, 4) if denominator else None
    recent_changes = changes[-10:]
    features['recent_direction_counts'] = {
        'up': sum(change > 0 for change in recent_changes),
        'down': sum(change < 0 for change in recent_changes),
        'unchanged': sum(change == 0 for change in recent_changes),
    }
    if mids:
        low, high = min(mids), max(mids)
        features['observed_midpoint_range'] = {'low': round(low, 4), 'high': round(high, 4)}
        features['current_range_position_pct'] = round((latest-low)/(high-low)*100, 2) if high != low else None
    span = ((datetime.fromisoformat(observations[-1]['exchange_time']) -
             datetime.fromisoformat(observations[0]['exchange_time'])).total_seconds()) if len(observations) > 1 else 0
    return {
        'basis': 'intraday_bid_ask_midpoint_snapshots_not_daily_bars',
        'observation_count': len(observations),
        'span_seconds': span,
        'sampling': 'Stored exchange-time quote snapshots; intervals may be irregular.',
        'features': features,
    }


def recent_decisions(ledger, symbol, limit=3):
    rows = ledger.db.execute('''SELECT timestamp,action,qty,reason,outcome,source
        FROM decisions WHERE symbol=? ORDER BY decision_id DESC LIMIT ?''', (symbol, limit)).fetchall()
    return [dict(timestamp=row['timestamp'], action=row['action'], qty=row['qty'],
                 reason=row['reason'], outcome=row['outcome'], source=row['source'])
            for row in reversed(rows)]


def fetch_news(symbol, name, at):
    """Fetch recent headline evidence; headlines are not treated as verified facts."""
    query = urllib.parse.urlencode({'q': f'"{name or symbol}" 台股 when:7d', 'hl': 'zh-TW', 'gl': 'TW', 'ceid': 'TW:zh-Hant'})
    url = 'https://news.google.com/rss/search?' + query
    req = urllib.request.Request(url, headers={'User-Agent': 'STOCKLLM-paper-research/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError('RSS_RESPONSE_TOO_LARGE')
        root = ET.fromstring(raw)
        headlines = []
        for item in root.findall('./channel/item')[:8]:
            try:
                published = parsedate_to_datetime(item.findtext('pubDate')).astimezone(at.tzinfo)
                if published > at:
                    continue
                publisher_node = item.find('source')
                headlines.append({
                    'symbol': symbol,
                    'headline': (item.findtext('title') or '')[:400],
                    'publisher': publisher_node.text if publisher_node is not None else 'unknown',
                    'published_at': published.isoformat(),
                    'first_seen_at': at.isoformat(),
                    'source': item.findtext('link', url),
                    'verified': False,
                    'scope': 'Headline only; article content and factual claims are not verified.',
                })
            except (TypeError, ValueError):
                continue
        return {'provider': 'Google News RSS', 'status': 'AVAILABLE' if headlines else 'NO_HEADLINES',
                'fetched_at': at.isoformat(), 'items': headlines}
    except Exception as exc:
        return {'provider': 'Google News RSS', 'status': 'UNAVAILABLE', 'fetched_at': at.isoformat(),
                'error_type': type(exc).__name__, 'items': []}


def news_memory(ledger, symbol, current, at, limit=40):
    """Combine this fetch with timestamped headlines already present in prior LLM inputs."""
    items = list(current)
    rows = ledger.db.execute('''SELECT input_json FROM strategy_cycles
        WHERE input_json IS NOT NULL AND input_json!='{}' ORDER BY timestamp DESC LIMIT 100''').fetchall()
    for row in rows:
        try:
            payload = json.loads(row['input_json'])
            for candidate in payload.get('candidates', []):
                if candidate.get('symbol') == symbol:
                    items.extend(candidate.get('news_context', {}).get('items', []))
        except (ValueError, TypeError, AttributeError):
            continue
    result = {'short': [], 'medium': [], 'long': []}
    seen = set()
    for item in items:
        try:
            published = datetime.fromisoformat(item['published_at']).astimezone(at.tzinfo)
            first_seen = datetime.fromisoformat(item['first_seen_at']).astimezone(at.tzinfo)
            available = max(published, first_seen)
            age_days = (at - available).total_seconds() / 86400
            key = item.get('source') or (item.get('headline'), item.get('published_at'))
            if available > at or age_days > 365 or key in seen:
                continue
            seen.add(key)
            tier = 'short' if age_days <= 2 else 'medium' if age_days <= 30 else 'long'
            result[tier].append({**item, 'available_at': available.isoformat()})
            if sum(map(len, result.values())) >= limit:
                break
        except (KeyError, TypeError, ValueError):
            continue
    return result


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
        quote_spread = (quote['ask'] - quote['bid']) if quote.get('ask') and quote.get('bid') else None
        quote_mid = ((quote['ask'] + quote['bid']) / 2) if quote.get('ask') and quote.get('bid') else None
        news = fetch_news(symbol, quote.get('name'), at)
        candidates.append(dict(symbol=symbol, quote=quote, sizing=sizing(portfolio, quote), observation_count=len(valid),
                               observation_span_seconds=span,
                               observed_return_pct=round((mids[-1] / mids[0] - 1) * 100, 4) if mids else None,
                               observed_return_basis='intraday_bid_ask_midpoint_snapshots_not_daily_bars',
                               quote_analysis=dict(midpoint=quote_mid, spread_twd=quote_spread,
                                   spread_pct=round(quote_spread/quote_mid*100, 4) if quote_spread is not None and quote_mid else None),
                               market_analysis=market_analysis(valid, mids),
                               recent_decisions=recent_decisions(ledger, symbol),
                               news_context=news,
                               news_memory=news_memory(ledger, symbol, news['items'], at),
                               evidence_ids=[q['evidence_id'] for q in valid],
                               observations=valid[-60:]))
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
