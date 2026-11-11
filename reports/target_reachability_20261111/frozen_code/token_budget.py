"""Lossless economic fields, compact repeated provenance for LLM transport only.

Persisted audit data and Python risk validation must use the original objects.
"""
from copy import deepcopy


def compact(value):
    if isinstance(value, list):
        return [compact(row) for row in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in ('daily_bars', 'market_history', 'price_levels') and isinstance(item, list):
            # Tabular encoding preserves every field/value and removes repeated
            # JSON keys. Mixed schemas stay untouched to preserve missing/null.
            if item and all(isinstance(b, dict) and set(b) == set(item[0]) for b in item):
                columns = list(item[0])
                result[key] = dict(columns=columns, rows=[[b[k] for k in columns] for b in item])
            else:
                result[key] = deepcopy(item)
        else:
            result[key] = compact(item)
    return deepcopy(result)


def candidate_view(candidate, account, rules, *, full_history=False):
    """Bound model transport; originals stay intact for validators and audit.

    Raw older rows remain accessible with get_research_data. Keep the latest
    five sessions, all price levels, provenance/status and original buy plans.
    """
    from entry_screen import entry_screen
    candidate = deepcopy(candidate)
    if candidate.get('opportunity_policy'):
        from opportunity_engine import refresh
        refresh(candidate, account or {}, rules)
    result = deepcopy(candidate)
    if not full_history and result.get('target_reachability'):
        from target_reachability import view
        result['target_reachability']=view(result['target_reachability'])
    metrics = result.get('metrics')
    if isinstance(metrics, dict):
        result['metrics'] = {key: value for key, value in metrics.items() if value is not None}
    if not full_history:
        # Redundant ranking explanations and long provenance URLs remain in
        # get_research_data and the original audit. Preserve risk/account facts.
        result.pop('components', None)
        if isinstance(result.get('position'), dict):
            result['position'] = {key: value for key, value in result['position'].items()
                                  if key in ('user_qty', 'ai_managed_qty', 'user_average_cost', 'total_qty', 'source')}
        (result.get('daily_trend') or {}).pop('note', None)
        status = result.get('daily_history_status') or {}
        if 'verified_trading_dates' in status:
            status['verified_calendar_sessions']=len(status.pop('verified_trading_dates'))
            status['full_calendar_tool']='get_research_data'
        if 'source_urls' in status:
            status['source_urls_available_via'] = 'get_research_data'
            status.pop('source_urls')
        for spec in (result.get('strategy_evidence') or {}).get('strategies', {}).values():
            (spec.get('observed') or {}).pop('interpretation', None)
        for key, limit in (('daily_bars', 5), ('market_history', 3)):
            rows = result.get(key) or []
            date_key = 'date' if key == 'daily_bars' else 'exchange_time'
            ordered = sorted(rows, key=lambda row: row.get(date_key, ''))
            result[key] = ordered[-limit:]
            result[key + '_coverage'] = dict(total_rows=len(rows), shown_rows=len(result[key]),
                truncated=len(rows) > limit, full_data_tool='get_research_data')
    result['entry_screen'] = entry_screen(candidate, account or {}, rules)
    return compact(result)


def decision_input(context):
    result = deepcopy(context.initial_input or {})
    result['analysis_set'] = [candidate_view(c, context.account, context.rules)
                              for c in context.candidates.values()]
    result['paper_account'] = deepcopy(context.account)
    result['available_buy_budget_twd'] = context.account.get('available_cash_twd')
    from collections import Counter
    history = context.history
    result['recent_trade_history'] = dict(recent=compact(history[:5]),
        sampled_attempts=len(history), status_counts=dict(Counter(row.get('status') for row in history)),
        full_data_tool='get_trade_history',
        note='嘗試次數不代表成交；持倉原計畫以各檔 buy_plans 的剩餘批次為準。')
    result['account_and_history_preloaded'] = True
    return result


def usage_snapshot(usage):
    return dict(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        requests=usage.requests,
        cached_input_tokens=getattr(usage.input_tokens_details, 'cached_tokens', 0),
        cache_write_tokens=getattr(usage.input_tokens_details, 'cache_write_tokens', 0),
        reasoning_tokens=getattr(usage.output_tokens_details, 'reasoning_tokens', 0))
