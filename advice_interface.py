"""Stable JSON handoff for downstream programs consuming dashboard advice."""
from datetime import datetime
import json
from pathlib import Path
from config import TAIPEI, now


def _age_seconds(exchange_time, at):
    if not exchange_time:
        return None
    try:
        return round((at - datetime.fromisoformat(exchange_time)).total_seconds(), 3)
    except (TypeError, ValueError):
        return None


def build_advice_interface(result, generated_at=None):
    """Convert one dashboard result into a compact, versioned consumer contract."""
    generated_at = generated_at or now()
    manifest = result.get('manifest') or {}
    batch = result.get('batch') or {}
    quotes = {q['symbol']: q for q in batch.get('quotes', [])}
    decisions = {d['symbol']: d for d in (result.get('decision') or {}).get('decisions', [])}

    market_quotes = []
    for symbol, quote in quotes.items():
        exchange_time = quote.get('quote_timestamp') or quote.get('exchange_time')
        market_quotes.append({
            'symbol': symbol,
            'source': quote.get('source'),
            'quote_status': quote.get('quote_status', quote.get('data_quality')),
            'price': quote.get('last_price', quote.get('price')),
            'bid': quote.get('bid'),
            'ask': quote.get('ask'),
            'volume': quote.get('volume_shares', quote.get('volume')),
            'volume_unit': quote.get('volume_unit', 'shares'),
            'is_trial': quote.get('is_trial'),
            'exchange_time': exchange_time,
            'received_at': quote.get('received_at') or quote.get('fetched_at'),
            'callback_received_at': quote.get('callback_received_at'),
            'age_seconds': _age_seconds(exchange_time, generated_at),
            'retrieval_method': quote.get('retrieval_method'),
            'warnings': quote.get('warnings') or [],
        })

    recommendations = []
    for candidate in result.get('analysis_set', []):
        symbol = candidate['symbol']
        decision = decisions.get(symbol, {})
        quote = quotes.get(symbol, {})
        position = candidate.get('position') or {}
        warnings = list(dict.fromkeys([*(quote.get('warnings') or []), *(decision.get('warnings') or [])]))
        recommendations.append({
            'symbol': symbol,
            'asset_type': candidate.get('asset_type', 'UNKNOWN'),
            'decision': decision.get('decision', 'WAIT'),
            'confidence': decision.get('confidence', 0),
            'reference_price': decision.get('reference_price'),
            'suggested_price': decision.get('suggested_price'),
            'suggested_qty': decision.get('suggested_qty'),
            'action_ratio': decision.get('action_ratio', 0),
            'reason': decision.get('reason', '本輪沒有有效的 LLM 建議。'),
            'warnings': warnings,
            'analysis_source': candidate.get('analysis_source'),
            'ai_managed_qty': position.get('ai_managed_qty'),
            'quote_status': quote.get('quote_status', quote.get('data_quality', 'UNAVAILABLE')),
            'quote_timestamp': quote.get('quote_timestamp') or quote.get('exchange_time'),
            'quote_age_seconds': _age_seconds(quote.get('quote_timestamp') or quote.get('exchange_time'), generated_at),
        })

    quality_counts = {}
    for quote in quotes.values():
        quality = quote.get('quote_status', quote.get('data_quality', 'UNAVAILABLE'))
        quality_counts[quality] = quality_counts.get(quality, 0) + 1
    validation = manifest.get('ai_validation', 'NOT_RECORDED')
    run_status = 'READY' if manifest.get('status') == 'COMPLETE' and validation == 'VALID' else 'ERROR'
    return {
        'interface_version': '1.0',
        'generated_at': generated_at.isoformat(timespec='seconds'),
        'run_id': manifest.get('run_id'),
        'run_status': run_status,
        'run_error': manifest.get('error') or None,
        'llm_validation': validation,
        'llm_model': manifest.get('llm_model'),
        'request_timing': {
            'market_started_at': manifest.get('request_started_at'),
            'market_completed_at': manifest.get('request_completed_at'),
            'llm_started_at': manifest.get('llm_request_started_at'),
            'llm_response_received_at': manifest.get('llm_response_received_at'),
            'run_finished_at': manifest.get('finished_at'),
        },
        'market': {
            'source': 'CAPITAL_SKCOM',
            'quote_basis': 'capital_intraday_odd_lot',
            'request_started_at': batch.get('request_timestamp') or manifest.get('request_started_at'),
            'request_completed_at': batch.get('request_completed_at') or manifest.get('request_completed_at'),
            'symbols_requested': batch.get('symbols_requested', manifest.get('quote_symbols_count', 0)),
            'symbols_received': batch.get('symbols_received', manifest.get('symbols_received', 0)),
            'quality_counts': quality_counts,
            'quotes': market_quotes,
        },
        'market_view': (result.get('decision') or {}).get('market_view', ''),
        'recommendations': recommendations,
        'paper_simulation': result.get('paper_simulation'),
        'real_order_sent': bool(manifest.get('real_order_sent', False)),
    }


def write_advice_interface(result, path, generated_at=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_advice_interface(result, generated_at)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)
    return payload

