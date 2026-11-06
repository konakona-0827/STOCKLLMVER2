"""Bounded daily participation; fills and outstanding orders are distinct."""
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from config import ROOT, TAIPEI, now

MARKER = 'DAILY_PARTICIPATION_PROBE'


def policy():
    path = ROOT / 'config/daily_participation.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'enabled': False}


def progress(path=None, at=None):
    at = at or now()
    day = at.astimezone(TAIPEI).date()
    result = dict(date=day.isoformat(), timezone='Asia/Taipei', status='READY',
                  filled={'BUY': 0, 'SELL': 0}, pending={'BUY': 0, 'SELL': 0})
    path = Path(path or ROOT/'data/execution/live_execution.sqlite3')
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            rows = db.execute('SELECT side,started_at,status,detail_json FROM execution_attempts').fetchall()
        for side, stamp, status, raw in rows:
            if side not in result['filled']:
                continue
            detail = json.loads(raw or '{}')
            submitted = datetime.fromisoformat(stamp)
            if submitted.tzinfo is None:
                raise ValueError('ORDER_TIMEZONE_UNKNOWN')
            filled = int(detail.get('filled_quantity') or 0)
            if submitted.astimezone(TAIPEI).date() == day and filled > 0:
                result['filled'][side] += 1
            # Any unresolved order prevents a duplicate probe, even across dates.
            if status in ('STARTED', 'SEND_PENDING', 'SUBMITTED', 'ACKNOWLEDGED',
                          'PARTIALLY_FILLED', 'UNCONFIRMED'):
                result['pending'][side] += 1
    except (OSError, sqlite3.Error, ValueError, TypeError):
        result['status'] = 'UNAVAILABLE'
    result['needed'] = [s for s in ('BUY', 'SELL') if not result['filled'][s] and not result['pending'][s]]
    return result


def probe_allowed(side, qty, price, *, state=None):
    rules = policy()
    state = state or progress()
    try:
        return (rules.get('enabled') is True and state['status'] == 'READY'
                and side in state['needed'] and type(qty) is int and qty == 1
                and Decimal('0') < Decimal(str(price)) <= Decimal(str(rules['max_notional_twd'])))
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return False


def add_probes(context, decision):
    """Supply missing one-share intents after model selection, before all guards."""
    from pretrade_policy import buy_block
    from scheduler import is_twse_oddlot_market_window
    import time
    state = progress()
    enabled = (policy().get('enabled') is True and
               ((context.initial_input or {}).get('daily_participation') or {}).get('policy', {}).get('enabled') is True)
    audit = dict(enabled=enabled, before=state, proposed=[], blocked=[])
    if (not ((context.initial_input or {}).get('daily_participation') or {}).get('policy', {}).get('enabled')
            or not policy().get('enabled') or context.read_only or not context.live_enabled
            or context.stopped() or state['status'] != 'READY'
            or not is_twse_oddlot_market_window(time.time())):
        return audit
    by_symbol = {d['symbol']: d for d in decision['decisions']}
    for side in (s for s in ('SELL', 'BUY') if s in state['needed']):
        if any(d['decision'] == side for d in decision['decisions']):
            continue
        for symbol, candidate in context.candidates.items():
            item = by_symbol.get(symbol)
            if not item or item['decision'] not in ('WAIT', 'HOLD'):
                continue
            if side == 'BUY' and context.support is not None and context.support.buy_block_reasons(symbol):
                continue
            quote = context.get_market_data(symbol)
            price = quote.get('ask' if side == 'BUY' else 'bid')
            if (quote.get('quote_status') != 'LIVE' or quote.get('is_trial')
                    or not probe_allowed(side, 1, price, state=state)):
                continue
            held = int((candidate.get('paper_position') or {}).get('qty') or 0)
            if side == 'SELL':
                if min(held, candidate.get('max_sell_qty', 0)) < 1:
                    continue
                plan = dict(strategy_id='NONE', target_price=None, invalidation_price=None, holding_days=None)
            else:
                if candidate.get('max_buy_qty', 0) < 1 or buy_block(candidate) or not candidate.get('eligible_strategies'):
                    continue
                levels = [r['price'] for r in candidate.get('price_levels', [])]
                above = [p for p in levels if p > price]
                below = [p for p in levels if p < price]
                if not above or not below:
                    continue
                plan = dict(strategy_id=candidate['eligible_strategies'][0], target_price=min(above),
                            invalidation_price=max(below), holding_days=1)
            item.update(decision=side, confidence=0.5, suggested_qty=1,
                        action_ratio=(1 / held if side == 'SELL' else 0),
                        reference_price=quote['last_price'], suggested_price=price,
                        data_quality='LIVE', **plan)
            # Ratios are floored by the shared validator. Avoid 1/49 rounding to zero.
            if side == 'SELL':
                item['action_ratio'] = min(1, (1.000000000001 / held))
            item['reason'] = f"{candidate.get('name', symbol)}（{symbol}）：每日參與試單 {side} 1 股；目的為小額驗證執行，費稅可能超過價差收益。"
            item['warnings'] = [MARKER, '試單非獲利訊號；仍須通過資金、可賣量、研究與風險上限檢查。']
            audit['proposed'].append(dict(symbol=symbol, side=side))
            break
        else:
            audit['blocked'].append(side + ':NO_ELIGIBLE_PROBE')
    return audit
