import json
import math
import os
import re


def load_universe(path):
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    symbols = data.get('symbols')
    if not isinstance(symbols, list) or not 10 <= len(symbols) <= 50:
        raise ValueError('Universe must contain 10..50 symbols')
    if any(not isinstance(s, str) or not re.fullmatch(r'(?:\d{4,6}|\d{4,5}[A-Z])', s) for s in symbols):
        raise ValueError('Invalid universe symbol')
    if len(set(symbols)) != len(symbols):
        raise ValueError('Duplicate universe symbols')
    etf_symbols = data.get('etf_symbols', [])
    if not isinstance(etf_symbols, list) or any(
            not isinstance(s, str) or s not in symbols for s in etf_symbols):
        raise ValueError('ETF symbols must be listed in universe symbols')
    if len(set(etf_symbols)) != len(etf_symbols):
        raise ValueError('Duplicate ETF symbol')
    data['etf_symbols'] = etf_symbols
    return data


def load_rules(path):
    rules = json.loads(path.read_text(encoding='utf-8-sig'))
    rules['candidate_top_n'] = int(os.getenv('CANDIDATE_TOP_N', rules['candidate_top_n']))
    if not 1 <= rules['candidate_top_n'] <= 10:
        raise ValueError('CANDIDATE_TOP_N must be 1..10')
    for key in ('min_volume_shares', 'min_turnover_proxy_twd', 'max_spread_pct',
                'momentum_return_scale', 'volume_reference_shares', 'liquidity_reference_twd'):
        if type(rules.get(key)) not in (int, float) or not math.isfinite(rules[key]) or rules[key] <= 0:
            raise ValueError('Invalid scan rule: ' + key)
    if set(rules['weights']) != {'momentum', 'volume', 'breakout', 'liquidity', 'spread_penalty', 'stale_penalty'}:
        raise ValueError('Invalid score components')
    for v in rules['weights'].values():
        if type(v) not in (int, float) or not math.isfinite(v) or v < 0:
            raise ValueError('Invalid component weight')
    return rules
