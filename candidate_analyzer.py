import json
import math
import os
from config import ROOT, now, redact


def object_schema(properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


SELECTED_SCHEMA = object_schema(dict(
    rank={'type': 'integer', 'minimum': 1, 'maximum': 5}, symbol={'type': 'string'},
    decision={'type': 'string', 'enum': ['BUY', 'HOLD', 'SELL', 'WAIT']},
    confidence={'type': 'number', 'minimum': 0, 'maximum': 1},
    reference_price={'type': 'number'}, suggested_price={'type': ['number', 'null']},
    reason={'type': 'string'}, risks={'type': 'array', 'items': {'type': 'string'}}))
REJECTED_SCHEMA = object_schema(dict(symbol={'type': 'string'}, reason={'type': 'string'}))
SCHEMA = object_schema(dict(market_view={'type': 'string'},
    selected={'type': 'array', 'maxItems': 5, 'items': SELECTED_SCHEMA},
    rejected_candidates={'type': 'array', 'items': REJECTED_SCHEMA},
    data_quality={'type': 'string', 'enum': ['LIVE', 'LAST_KNOWN', 'MIXED', 'UNAVAILABLE']}))


def overall_quality(candidates):
    values = {q['quote_status'] for q in candidates}
    return next(iter(values)) if len(values) == 1 else ('MIXED' if values else 'UNAVAILABLE')


def build_request(candidates):
    payload = dict(purpose='RESEARCH_SELECTION_ONLY_NO_ORDERS', candidates=candidates,
                   overall_data_quality=overall_quality(candidates), max_selected=5,
                   daily_history_status='INSUFFICIENT_VALIDATED_DAILY_BARS')
    return dict(model=os.getenv('OPENAI_MODEL', 'gpt-5.6-luna'), store=False,
                instructions=(ROOT/'prompt_selection.md').read_text(encoding='utf-8'),
                input=json.dumps(payload, ensure_ascii=False, allow_nan=False),
                text={'format': {'type': 'json_schema', 'name': 'candidate_selection', 'strict': True, 'schema': SCHEMA}},
                max_output_tokens=5000)


def validate(result, candidates):
    if not isinstance(result, dict) or set(result) != set(SCHEMA['required']):
        raise ValueError('INVALID_SELECTION_FIELDS')
    if not isinstance(result['market_view'], str) or not result['market_view'].strip():
        raise ValueError('INVALID_MARKET_VIEW')
    if result['data_quality'] != overall_quality(candidates):
        raise ValueError('INCORRECT_DATA_QUALITY')
    if not isinstance(result['selected'], list) or len(result['selected']) > 5 or not isinstance(result['rejected_candidates'], list):
        raise ValueError('INVALID_SELECTION_LIST')
    lookup, seen = {c['symbol']: c for c in candidates}, set()
    for rank, item in enumerate(result['selected'], 1):
        if not isinstance(item, dict) or set(item) != set(SELECTED_SCHEMA['required']):
            raise ValueError('INVALID_SELECTED_FIELDS')
        symbol = item['symbol']
        if not isinstance(symbol, str) or symbol not in lookup or symbol in seen:
            raise ValueError('UNKNOWN_OR_DUPLICATE_SYMBOL')
        seen.add(symbol)
        if type(item['rank']) is not int or item['rank'] != rank:
            raise ValueError('INVALID_RANK')
        if item['decision'] not in {'BUY', 'HOLD', 'SELL', 'WAIT'}:
            raise ValueError('INVALID_DECISION')
        c = lookup[symbol]
        if item['decision'] in {'HOLD', 'SELL'} and not (c.get('position_qty') or 0) > 0:
            raise ValueError('POSITION_NOT_CONFIRMED')
        conf = item['confidence']
        if type(conf) not in (int, float) or not math.isfinite(conf) or not 0 <= conf <= 1:
            raise ValueError('INVALID_CONFIDENCE')
        ref, price = item['reference_price'], item['suggested_price']
        if type(ref) not in (int, float) or not math.isfinite(ref) or abs(ref-c['last_price']) > 1e-8:
            raise ValueError('INVENTED_REFERENCE_PRICE')
        if price is not None and (type(price) not in (int, float) or not math.isfinite(price) or price <= 0):
            raise ValueError('INVALID_SUGGESTED_PRICE')
        if item['decision'] in {'WAIT', 'HOLD'} and price is not None:
            raise ValueError('NON_ACTION_PRICE')
        if not isinstance(item['reason'], str) or not item['reason'].strip():
            raise ValueError('INVALID_REASON')
        if not isinstance(item['risks'], list) or any(not isinstance(v, str) for v in item['risks']):
            raise ValueError('INVALID_RISKS')
        if c['quote_status'] == 'LAST_KNOWN' and not any('不是即時行情' in v for v in item['risks']):
            raise ValueError('MISSING_STALE_WARNING')
    for item in result['rejected_candidates']:
        if not isinstance(item, dict) or set(item) != {'symbol', 'reason'}:
            raise ValueError('INVALID_REJECTION')
        if not isinstance(item['symbol'], str) or item['symbol'] not in lookup or item['symbol'] in seen:
            raise ValueError('INVALID_REJECTED_SYMBOL')
        if not isinstance(item['reason'], str) or not item['reason'].strip():
            raise ValueError('INVALID_REJECTION_REASON')
        seen.add(item['symbol'])
    if seen != set(lookup):
        raise ValueError('UNACCOUNTED_CANDIDATE')
    return result


def call_openai(request):
    from openai import OpenAI
    with OpenAI(api_key=os.environ.get('OPENAI_API_KEY'), timeout=60, max_retries=0) as client:
        response = client.responses.create(**request)
    return response.output_text, dict(response_id=response.id, status=response.status,
                                      usage=response.usage.model_dump() if response.usage else {},
                                      response_object=response.model_dump())


def analyze(candidates, request, provider=call_openai):
    started, raw, api = now().isoformat(), '', {}
    try:
        raw, api = provider(request)
        result = validate(json.loads(raw), candidates)
        validation = 'VALID'
    except Exception as exc:
        api['error'] = {'type': type(exc).__name__, 'detail': redact(exc)}
        validation = 'ERROR'
        result = dict(market_view='WAIT：OpenAI或JSON驗證失敗，沒有產生有效候選建議。', selected=[],
                      rejected_candidates=[{'symbol': c['symbol'], 'reason': 'ANALYSIS_FAILED_WAIT'} for c in candidates],
                      data_quality=overall_quality(candidates))
    names = {c['symbol']: c.get('name') or '股票名稱未取得' for c in candidates}
    for item in result['selected']:
        name = names.get(item['symbol'], '股票名稱未取得')
        prefix = f"{name}（{item['symbol']}）："
        if not item['reason'].startswith(prefix):
            item['reason'] = prefix + item['reason']
    for item in result['rejected_candidates']:
        name = names.get(item['symbol'], '股票名稱未取得')
        prefix = f"{name}（{item['symbol']}）："
        if not item['reason'].startswith(prefix):
            item['reason'] = prefix + item['reason']
    return result, dict(request_started_at=started, response_received_at=now().isoformat(),
                        validation=validation, raw_response=redact(raw), api=api)
