"""Union-wide structured advice. No execution or position mutation."""
import json
import math
import os
from decimal import Decimal, ROUND_FLOOR
from config import ROOT, now, redact
from candidate_analyzer import object_schema, call_openai

ITEM = object_schema(dict(
    symbol={'type': 'string'}, decision={'type': 'string', 'enum': ['BUY', 'HOLD', 'SELL', 'WAIT']},
    confidence={'type': 'number', 'minimum': 0, 'maximum': 1},
    action_ratio={'type': 'number', 'minimum': 0, 'maximum': 1},
    suggested_qty={'type': ['integer', 'null'], 'minimum': 0},
    reference_price={'type': ['number', 'null']}, suggested_price={'type': ['number', 'null']},
    reason={'type': 'string'}, data_quality={'type': 'string', 'enum': ['LIVE', 'LAST_KNOWN', 'UNAVAILABLE']},
    warnings={'type': 'array', 'items': {'type': 'string'}}))
SCHEMA = object_schema(dict(market_view={'type': 'string'}, decisions={'type': 'array', 'items': ITEM}))


def sell_qty(qty, ratio):
    return int((Decimal(qty)*Decimal(str(ratio))).to_integral_value(rounding=ROUND_FLOOR))


def build_request(candidates):
    return dict(model=os.getenv('OPENAI_MODEL', 'gpt-5.6-luna'), store=False,
                instructions=(ROOT/'prompt_positions.md').read_text(encoding='utf-8'),
                input=json.dumps(dict(purpose='ADVICE_ONLY_NO_ORDERS', analysis_set=candidates,
                                     available_buy_budget_twd=None), ensure_ascii=False, allow_nan=False),
                text={'format': {'type': 'json_schema', 'name': 'position_advice', 'strict': True, 'schema': SCHEMA}},
                max_output_tokens=min(24000, max(6000, len(candidates)*650)))


def validate(result, candidates):
    if not isinstance(result, dict) or set(result) != set(SCHEMA['required']) or not isinstance(result['market_view'], str) or not result['market_view'].strip():
        raise ValueError('INVALID_ROOT')
    if not isinstance(result['decisions'], list):
        raise ValueError('INVALID_DECISIONS')
    lookup, seen = {c['symbol']: c for c in candidates}, set()
    for d in result['decisions']:
        if not isinstance(d, dict) or set(d) != set(ITEM['required']):
            raise ValueError('INVALID_FIELDS')
        s = d['symbol']
        if not isinstance(s, str) or s not in lookup or s in seen:
            raise ValueError('UNKNOWN_OR_DUPLICATE_SYMBOL')
        seen.add(s)
        c = lookup[s]
        qty = c['position']['ai_managed_qty']
        if d['decision'] not in ('BUY', 'HOLD', 'SELL', 'WAIT') or (qty == 0 and d['decision'] in ('HOLD', 'SELL')):
            raise ValueError('INVALID_POSITION_DECISION')
        for key in ('confidence', 'action_ratio'):
            if type(d[key]) not in (int, float) or not math.isfinite(d[key]) or not 0 <= d[key] <= 1:
                raise ValueError('INVALID_' + key)
        if d['suggested_qty'] is not None and (type(d['suggested_qty']) is not int or d['suggested_qty'] < 0):
            raise ValueError('INVALID_QTY')
        if d['decision'] == 'SELL':
            expected = sell_qty(qty, d['action_ratio'])
            if expected <= 0 or d['suggested_qty'] != expected:
                raise ValueError('SELL_REJECTED_LOCAL_OWNERSHIP_OR_RATIO')
        elif d['decision'] == 'BUY':
            if d['suggested_qty'] is not None or d['action_ratio'] != 0:
                raise ValueError('BUY_BUDGET_NOT_PROVIDED')
        elif d['action_ratio'] != 0 or d['suggested_qty'] != 0 or d['suggested_price'] is not None:
            raise ValueError('NON_ACTION_QUANTITY')
        ref = d['reference_price']
        if c['last_price'] is None:
            if ref is not None:
                raise ValueError('INVENTED_REFERENCE')
        elif type(ref) not in (int, float) or not math.isfinite(ref) or abs(ref-c['last_price']) > 1e-8:
            raise ValueError('INCORRECT_REFERENCE')
        price = d['suggested_price']
        if price is not None and (type(price) not in (int, float) or not math.isfinite(price) or price <= 0):
            raise ValueError('INVALID_PRICE')
        if d['data_quality'] != c['quote_status'] or (c['quote_status'] == 'UNAVAILABLE' and d['decision'] != 'WAIT'):
            raise ValueError('INVALID_DATA_QUALITY_DECISION')
        if not isinstance(d['reason'], str) or not d['reason'].strip() or not isinstance(d['warnings'], list) or any(not isinstance(w, str) for w in d['warnings']):
            raise ValueError('INVALID_EXPLANATION')
        if c['quote_status'] == 'LAST_KNOWN' and not any('不是即時行情' in w for w in d['warnings']):
            raise ValueError('MISSING_STALE_WARNING')
        if d['decision'] == 'BUY' and not any('預算' in w for w in d['warnings']):
            raise ValueError('MISSING_BUDGET_WARNING')
    if seen != set(lookup):
        raise ValueError('MISSING_ANALYSIS_SYMBOL')
    return result


def analyze(candidates, request, provider=call_openai):
    started, raw, api = now().isoformat(), '', {}
    try:
        raw, api = provider(request)
        result = validate(json.loads(raw), candidates)
        validation = 'VALID'
    except Exception as exc:
        api['error'] = redact(f'{type(exc).__name__}: {exc}')
        validation = 'ERROR'
        result = dict(market_view='分析失敗，本輪全部 WAIT。', decisions=[dict(
            symbol=c['symbol'], decision='WAIT', confidence=0, action_ratio=0, suggested_qty=0,
            reference_price=c['last_price'], suggested_price=None, reason='ANALYSIS_FAILED_WAIT',
            data_quality=c['quote_status'], warnings=['ANALYSIS_FAILED'] +
            (['不是即時行情'] if c['quote_status']=='LAST_KNOWN' else [])) for c in candidates])
    return result, dict(request_started_at=started, response_received_at=now().isoformat(),
                        validation=validation, raw_response=redact(raw), api=api)
