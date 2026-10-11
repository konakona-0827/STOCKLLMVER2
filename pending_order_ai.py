"""AI review of this application's verified, still open intraday odd-lot orders."""
import json
import math
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation

from candidate_analyzer import call_openai, object_schema
from config import TAIPEI, now, redact


ITEM = object_schema(dict(
    decision_id={'type': 'string'},
    action={'type': 'string', 'enum': ['KEEP', 'CANCEL']},
    confidence={'type': 'number', 'minimum': 0, 'maximum': 1},
    reason={'type': 'string'},
))
SCHEMA = object_schema(dict(decisions={'type': 'array', 'items': ITEM}))

INSTRUCTIONS = """你管理本程式已送出、尚未完全成交的台灣盤中零股委託。
逐筆決定 KEEP 或 CANCEL。CANCEL 只表示要求取消尚未成交的餘量，已成交股數不會逆轉。
根據委託方向、原限價、餘量、目前買賣價與持倉風險判斷。不要為了釋放預算而機械地撤單。
若資訊不足、價格過期、委託身分或狀態不明，選 KEEP 並說明原因。
不要假設刪單請求已成功；券商取消回報才可確認。每個 decision_id 恰好回答一次。
"""


def eligible_orders(health, quotes, at):
    """Only exact, same-day broker replay evidence can reach the model."""
    if (health.get('status') not in ('OK', 'PENDING_ORDER')
            or health.get('errors') or health.get('reconciliation_errors')
            or health.get('pending_buy_reserve_error')):
        return []
    qmap = {q['symbol']: q for q in quotes}
    inventory = {r['symbol']: r for r in health.get('inventory_comparison') or []}
    day = at.astimezone(TAIPEI).date()
    orders = []
    seen = set()
    for row in health.get('broker_order_reconciliation') or []:
        try:
            started = datetime.fromisoformat(row['started_at'])
            seq = str(row['seq13'])
            remaining = int(row['unconfirmed_qty'])
            ordered = int(row['ordered_qty'])
            filled = int(row['broker_verified_filled_qty'])
            limit_price = Decimal(str(row['limit_price_twd']))
            quote = qmap[row['symbol']]
            if (started.astimezone(TAIPEI).date() != day
                    or len(seq) != 13 or not seq.isdecimal()
                    or row['decision_id'] in seen
                    or row.get('cancel_requested') is True
                    or row['side'] not in ('BUY', 'SELL')
                    or row['ledger_status'] not in ('ACKNOWLEDGED', 'PARTIALLY_FILLED')
                    or row['status'] not in ('NO_FILL_YET_VERIFIED', 'FILL_MATCH')
                    or row['broker_event_status'] not in
                       ('ACKNOWLEDGED_NO_FILL_EVENT', 'PARTIALLY_FILLED_REMAINDER_UNCONFIRMED')
                    or row['broker_replay_complete'] is not True
                    or int(row['broker_event_counts']['ack']) < 1
                    or int(row['broker_event_counts']['cancel']) != 0
                    or not 0 < remaining == ordered - filled
                    or not limit_price.is_finite() or limit_price <= 0
                    or quote.get('quote_status') != 'LIVE'
                    or quote.get('is_trial')):
                continue
            seen.add(row['decision_id'])
            orders.append(dict(
                decision_id=row['decision_id'], seq13=seq,
                symbol=row['symbol'], side=row['side'], started_at=row['started_at'],
                ordered_qty=ordered, filled_qty=filled, remaining_qty=remaining,
                limit_price_twd=row['limit_price_twd'],
                last_price=quote.get('last_price'), bid=quote.get('bid'),
                ask=quote.get('ask'), quote_timestamp=quote.get('quote_timestamp'),
                quote_status=quote['quote_status'],
                ai_managed_qty=(inventory.get(row['symbol']) or {}).get('ai_qty'),
                broker_cash_qty=(inventory.get(row['symbol']) or {}).get('broker_cash_qty'),
                pending_buy_reserve_twd=health.get('pending_buy_reserve_twd'),
            ))
        except (KeyError, TypeError, ValueError, OverflowError, InvalidOperation):
            continue
    return orders


def build_request(orders, strategy_prompt='', model=None):
    return dict(
        model=model or os.getenv('OPENAI_MODEL', 'gpt-5.6-luna'), store=False,
        instructions=INSTRUCTIONS + '\n目前策略：\n' + strategy_prompt,
        input=json.dumps({'pending_orders': orders}, ensure_ascii=False, allow_nan=False),
        text={'format': {'type': 'json_schema', 'name': 'pending_order_review',
                         'strict': True, 'schema': SCHEMA}},
        max_output_tokens=max(1500, min(6000, len(orders) * 350)),
    )


def validate(result, orders):
    if not isinstance(result, dict) or set(result) != {'decisions'} or not isinstance(result['decisions'], list):
        raise ValueError('INVALID_CANCEL_RESPONSE')
    expected = {o['decision_id'] for o in orders}
    seen = set()
    for item in result['decisions']:
        if not isinstance(item, dict) or set(item) != set(ITEM['required']):
            raise ValueError('INVALID_CANCEL_FIELDS')
        key = item['decision_id']
        if key not in expected or key in seen or item['action'] not in ('KEEP', 'CANCEL'):
            raise ValueError('UNKNOWN_OR_DUPLICATE_ORDER')
        if (type(item['confidence']) not in (int, float)
                or not math.isfinite(item['confidence'])
                or not 0 <= item['confidence'] <= 1
                or not isinstance(item['reason'], str) or not item['reason'].strip()):
            raise ValueError('INVALID_CANCEL_REASON_OR_CONFIDENCE')
        seen.add(key)
    if seen != expected:
        raise ValueError('MISSING_ORDER_DECISION')
    return result


def analyze(orders, strategy_prompt='', provider=call_openai):
    request = build_request(orders, strategy_prompt)
    try:
        raw, api = provider(request)
        decision = validate(json.loads(raw), orders)
        validation = 'VALID'
    except Exception as exc:
        api = {'error': redact(f'{type(exc).__name__}: {exc}')}
        decision = {'decisions': [dict(decision_id=o['decision_id'], action='KEEP',
                                      confidence=0, reason='AI_REVIEW_FAILED')
                                  for o in orders]}
        validation = 'ERROR'
    return request, decision, {'validation': validation, 'api': api,
                               'response_received_at': now().isoformat()}
