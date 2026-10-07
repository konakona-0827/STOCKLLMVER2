"""Responses API pattern reused from stockprototype/stockprototype/llm.py."""
import json
import os
from config import ROOT

ACTION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {'action': {'type': 'string', 'enum': ['BUY', 'SELL', 'HOLD', 'NO_TRADE', 'REVIEW']},
                   'symbol': {'type': 'string'}, 'qty': {'type': 'integer', 'minimum': 0, 'maximum': 999},
                   'reason': {'type': 'string'}},
    'required': ['action', 'symbol', 'qty', 'reason']}
SCHEMA = {'type': 'object', 'additionalProperties': False,
          'properties': {'summary': {'type': 'string'},
                         'actions': {'type': 'array', 'items': ACTION_SCHEMA, 'maxItems': 3}},
          'required': ['summary', 'actions']}


def validate(payload, symbols):
    if not isinstance(payload, dict) or not isinstance(payload.get('summary'), str):
        raise ValueError('INVALID_SUMMARY')
    actions = payload.get('actions')
    if not isinstance(actions, list) or len(actions) > 3:
        raise ValueError('INVALID_ACTIONS')
    seen = set()
    for item in actions:
        if not isinstance(item, dict):
            raise ValueError('INVALID_ACTION')
        action, symbol, qty = item.get('action'), item.get('symbol'), item.get('qty')
        if action not in {'BUY', 'SELL', 'HOLD', 'NO_TRADE', 'REVIEW'}:
            raise ValueError('INVALID_ACTION')
        if symbol not in symbols or symbol in seen:
            raise ValueError('INVALID_OR_DUPLICATE_SYMBOL')
        if type(qty) is not int or not 0 <= qty <= 999 or not isinstance(item.get('reason'), str):
            raise ValueError('INVALID_LOTS_OR_REASON')
        if (action in {'BUY', 'SELL'}) != (qty > 0):
            raise ValueError('INVALID_ACTION_QUANTITY')
        seen.add(symbol)
    return payload


def decide(payload):
    from openai import OpenAI
    key = os.environ.get('OPENAI_API_KEY')
    if not key:
        raise ValueError('OPENAI_API_KEY_MISSING')
    model = os.getenv('OPENAI_MODEL', 'gpt-5.6-luna')
    with OpenAI(api_key=key, timeout=45, max_retries=0) as client:
        response = client.responses.create(
            model=model, store=False,
            instructions=(ROOT / 'prompt.md').read_text(encoding='utf-8-sig'),
            input=json.dumps(payload, ensure_ascii=False),
            text={'format': {'type': 'json_schema', 'name': 'paper_decision',
                             'strict': True, 'schema': SCHEMA}},
            max_output_tokens=3000)
    usage = response.usage.model_dump() if response.usage else {}
    return response.output_text, {'model': model, 'usage': usage, 'response_status': response.status,
                                  'response_id': response.id}
