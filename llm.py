"""One structured OpenAI analysis; no execution or portfolio accounting."""
import json
import math
import os
from config import ROOT

SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'decision': {'type': 'string', 'enum': ['BUY', 'HOLD', 'SELL', 'WAIT']},
        'symbol': {'type': 'string'},
        'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
        'reason': {'type': 'string'},
        'reference_price': {'type': ['number', 'null']},
        'suggested_price': {'type': ['number', 'null']},
        'suggested_qty': {'type': ['integer', 'null']},
        'data_quality': {'type': 'string', 'enum': ['LIVE', 'LAST_KNOWN', 'UNAVAILABLE']},
        'warnings': {'type': 'array', 'items': {'type': 'string'}}},
    'required': ['decision', 'symbol', 'confidence', 'reason', 'reference_price',
                 'suggested_price', 'suggested_qty', 'data_quality', 'warnings']}


def fallback(snapshot, reason, warning):
    return dict(decision='WAIT', symbol=snapshot['symbol'], confidence=0.0, reason=reason,
                reference_price=snapshot.get('price'), suggested_price=None, suggested_qty=None,
                data_quality=snapshot['data_quality'], warnings=[*snapshot.get('warnings', []), warning])


def validate(payload, snapshot, held_qty=None):
    if not isinstance(payload, dict) or set(payload) != set(SCHEMA['required']):
        raise ValueError('INVALID_JSON_FIELDS')
    if payload['decision'] not in {'BUY', 'HOLD', 'SELL', 'WAIT'} or payload['symbol'] != snapshot['symbol']:
        raise ValueError('INVALID_DECISION_OR_SYMBOL')
    confidence = payload['confidence']
    if type(confidence) not in (float, int) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError('INVALID_CONFIDENCE')
    if not isinstance(payload['reason'], str) or not payload['reason'].strip():
        raise ValueError('INVALID_REASON')
    if not isinstance(payload['warnings'], list) or any(not isinstance(w, str) for w in payload['warnings']):
        raise ValueError('INVALID_WARNINGS')
    if payload['data_quality'] not in {'LIVE', 'LAST_KNOWN', 'UNAVAILABLE'}:
        raise ValueError('INVALID_DATA_QUALITY')
    for key in ('reference_price', 'suggested_price'):
        value = payload[key]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
            raise ValueError('INVALID_PRICE')
    qty = payload['suggested_qty']
    if qty is not None and (type(qty) is not int or not 1 <= qty <= 999):
        raise ValueError('INVALID_SUGGESTED_QTY')
    result = dict(payload)
    result['warnings'] = list(dict.fromkeys([*snapshot.get('warnings', []), *payload['warnings']]))
    result['data_quality'] = snapshot['data_quality']
    # Reference is a supplied observed last trade, never an invented model price.
    result['reference_price'] = snapshot.get('price')
    if snapshot['data_quality'] == 'UNAVAILABLE':
        result.update(decision='WAIT', confidence=0.0, reference_price=None, suggested_price=None,
                      suggested_qty=None, reason='沒有可用行情，等待資料；不推測價格或交易動作。')
    if snapshot['data_quality'] == 'LAST_KNOWN':
        result['warnings'].append('這是最後已知行情，不是即時行情；請勿將價格視為目前可成交價格。')
    if result['decision'] in {'SELL', 'HOLD'} and (held_qty is None or held_qty <= 0):
        result.update(decision='WAIT', suggested_qty=None, suggested_price=None,
                      reason='未提供已持倉資訊，無法確認持有或賣出建議適用。')
        result['warnings'].append('POSITION_NOT_CONFIRMED')
    if result['decision'] == 'SELL' and qty is not None and qty > held_qty:
        raise ValueError('SUGGESTED_SELL_EXCEEDS_PROVIDED_POSITION')
    if result['decision'] in {'HOLD', 'WAIT'}:
        result['suggested_qty'] = result['suggested_price'] = None
    result['warnings'] = list(dict.fromkeys(result['warnings']))
    return result


def build_request(payload):
    return dict(model=os.getenv('OPENAI_MODEL', 'gpt-5.6-luna'), store=False,
                instructions=(ROOT / 'prompt.md').read_text(encoding='utf-8-sig'),
                input=json.dumps(payload, ensure_ascii=False),
                text={'format': {'type': 'json_schema', 'name': 'ai_trading_advice', 'strict': True, 'schema': SCHEMA}},
                max_output_tokens=2500)


def decide(payload):
    from openai import OpenAI
    key = os.getenv('OPENAI_API_KEY')
    if not key:
        raise ValueError('OPENAI_API_KEY_MISSING')
    model = os.getenv('OPENAI_MODEL', 'gpt-5.6-luna')
    with OpenAI(api_key=key, timeout=45, max_retries=0) as client:
        response = client.responses.create(**build_request(payload))
    return response.output_text, {'model': model, 'response_status': response.status, 'response_id': response.id,
                                 'response_object': response.model_dump(),
                                 'usage': response.usage.model_dump() if response.usage else {}}
