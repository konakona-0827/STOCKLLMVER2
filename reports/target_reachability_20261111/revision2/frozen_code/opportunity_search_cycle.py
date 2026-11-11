"""Visit distinct research candidates until an action or the observed pool ends.

Rejections never become approval. Each new batch goes through the complete
agent, specialist, fresh-quote and pretrade checks supplied by analyze_batch.
"""
from copy import deepcopy

from opportunity_engine import select

USAGE_KEYS = ('input_tokens', 'output_tokens', 'requests', 'cached_input_tokens',
              'cache_write_tokens', 'reasoning_tokens')


def run(pool, initial, account, rules, policy, analyze_batch, *, expand=None,
        stopped=lambda: False, read_only=False):
    visited = set()
    observed = {c['symbol'] for c in pool}
    handoff, decisions, responses, rounds, expansions = [], [], [], [], []
    views = []
    batch = initial
    stop_reason = 'OBSERVED_POOL_EXHAUSTED'
    while not stopped():
        if account.get('budget_status', 'READY') != 'READY' and not read_only:
            stop_reason = 'ACCOUNT_NOT_READY'
            break
        if not batch:
            remaining = [c for c in pool if c['symbol'] not in visited]
            batch, _ = select(remaining, account, rules, policy)
        if not batch:
            if expand is None or read_only or account.get('budget_status', 'READY') != 'READY':
                break
            known = set(observed)
            try:
                expansion = expand(known, deepcopy(decisions))
            except Exception as exc:
                from config import redact
                expansion = dict(status='ALTERNATIVE_SEARCH_FAILED', error=redact(exc), candidates=[])
            expansions.append({k: v for k, v in expansion.items() if k != 'candidates'})
            additions = []
            for candidate in expansion.get('candidates', []):
                if candidate['symbol'] not in known:
                    known.add(candidate['symbol'])
                    additions.append(candidate)
            newly_observed = (set(expansion.get('observed_symbols', [])) | {c['symbol'] for c in additions}) - observed
            observed.update(newly_observed)
            if not additions:
                if newly_observed:
                    continue
                stop_reason = expansion.get('status') or 'NO_NEW_CANDIDATES'
                break
            pool.extend(additions)
            continue
        batch = [c for c in batch if c['symbol'] not in visited]
        if not batch:
            stop_reason = 'NO_UNREVIEWED_CANDIDATES'
            break
        symbols = [c['symbol'] for c in batch]
        # Mark before dispatch: failed/negative research is not retried with
        # the same facts just to solicit a different answer.
        visited.update(symbols)
        decision, response = analyze_batch(batch, len(rounds))
        views.append(decision['market_view'])
        handoff.extend(batch)
        decisions.extend(decision.get('decisions', []))
        responses.append(response)
        rounds.append(dict(symbols=symbols, validation=response['validation'],
            decisions=[dict(symbol=d['symbol'], decision=d['decision'], reason=d['reason'])
                       for d in decision.get('decisions', [])]))
        if response['validation'] != 'VALID':
            stop_reason = 'RESEARCH_FAILED'
            break
        if any(d['decision'] in ('BUY', 'SELL') for d in decision.get('decisions', [])):
            # Dispatch is still owned by the caller; do not combine separately
            # approved BUY batches against the same unreserved cash.
            stop_reason = 'REVIEWED_TRADE_INTENT_PRODUCED'
            break
        if read_only:
            stop_reason = 'READ_ONLY_REVIEW_COMPLETE'
            break
        batch = []
    if stopped():
        stop_reason = 'USER_STOPPED'
    response = merge_responses(responses)
    response['candidate_search'] = dict(scope='OBSERVED_AND_SCOUTED_POOL_NOT_WHOLE_MARKET',
        rounds=rounds, expansions=expansions, visited_symbols=sorted(visited),
        pool_count=len(pool), stop_reason=stop_reason)
    result = dict(market_view='；'.join(views) or '已完成可觀測候選搜尋。',
                  decisions=decisions)
    return result, response, handoff


def merge_responses(responses):
    if not responses:
        return dict(validation='NOT_CALLED', api={}, raw_response='', tool_calls=[])
    response = deepcopy(responses[0])
    if len(responses) == 1:
        return response
    response['validation'] = 'ERROR' if any(r['validation'] != 'VALID' for r in responses) else 'VALID'
    response['response_received_at'] = responses[-1].get('response_received_at')
    response['tool_calls'] = [call for r in responses for call in r.get('tool_calls', [])]
    api = response.setdefault('api', {})
    api['usage'] = {k: sum((r.get('api', {}).get('usage') or {}).get(k, 0) or 0 for r in responses)
                    for k in USAGE_KEYS}
    api['usage_accounting_complete'] = all(r.get('api', {}).get('usage_accounting_complete', True)
                                          for r in responses)
    seen, usage = set(), []
    for r in responses:
        for row in r.get('api', {}).get('usage_requests', []):
            response_id = row.get('response_id')
            if response_id and response_id in seen:
                continue
            if response_id:
                seen.add(response_id)
            usage.append(row)
    api['usage_requests'] = usage
    if usage and all(r.get('api', {}).get('usage_requests') or not r.get('api', {}).get('usage')
                     for r in responses):
        api['usage'] = {k: sum(row.get(k, 0) or 0 for row in usage) for k in USAGE_KEYS}
    for key in ['support_research', 'pretrade_review']:
        if any(r.get(key) for r in responses):
            field = 'calls' if key == 'support_research' else 'reviews'
            merged = deepcopy(next(r[key] for r in responses if r.get(key)))
            merged[field] = [item for r in responses for item in (r.get(key) or {}).get(field, [])]
            if key == 'pretrade_review' and merged[field]:
                merged['status'] = 'VALID' if all((r.get(key) or {}).get('status') in ('VALID', 'NOT_NEEDED')
                                                for r in responses) else 'ERROR'
                review_apis = [(r.get(key) or {}).get('api') or {} for r in responses]
                merged['api'] = deepcopy(next((a for a in review_apis if a), {}))
                merged['api']['usage'] = {k: sum((a.get('usage') or {}).get(k, 0) or 0 for a in review_apis)
                                           for k in USAGE_KEYS}
                merged['api']['usage_accounting_complete'] = all(a.get('usage_accounting_complete', True)
                                                                 for a in review_apis)
            response[key] = merged
    errors = [r.get('api', {}).get('error') for r in responses if r.get('api', {}).get('error')]
    if errors:
        api['error'] = '；'.join(errors)
    return response
