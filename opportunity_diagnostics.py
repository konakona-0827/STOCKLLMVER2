"""Separate observed pipeline outcomes from claims about market opportunity."""
from collections import Counter


def summarize(batch, ranked, pool, selected, search, discovery, response, decision, account,
              execution=None):
    quotes=batch.get('quotes',[])
    buys=[d for d in decision.get('decisions',[]) if d.get('decision')=='BUY']
    counts=dict(quoted=len(quotes),live_non_trial=sum(q.get('quote_status')=='LIVE' and
        not q.get('is_trial') for q in quotes),quant_eligible=len(ranked),entry_pool=len(pool),
        qualified_entries=len(search.get('qualified_symbols',[])),
        watch_research=len(search.get('watch_research_symbols',[])),
        model_candidates=len(selected) if response.get('validation')!='NOT_CALLED' else 0,
        final_buy_decisions=len(buys))
    reasons=Counter(reason for c in pool for reason in
                    (c.get('opportunity_status') or {}).get('blockers',[]))
    if account.get('budget_status')!='READY':outcome='ACCOUNT_OR_BROKER_BLOCKED'
    elif not counts['live_non_trial']:outcome='LIVE_MARKET_DATA_UNAVAILABLE'
    elif response.get('validation')=='ERROR':outcome='DECISION_RESEARCH_FAILED'
    elif buys:outcome='BUY_DECISION_PRODUCED'
    elif (response.get('candidate_search') or {}).get('stop_reason') in (
            'ALTERNATIVE_SEARCH_FAILED','UNAVAILABLE','SCOUT_RETRY_DEFERRED','CACHED_AFTER_FAILURE'):
        outcome='MARKET_DISCOVERY_INCOMPLETE'
    elif (discovery or {}).get('status') in ('UNAVAILABLE','SCOUT_RETRY_DEFERRED','CACHED_AFTER_FAILURE'):
        outcome='MARKET_DISCOVERY_INCOMPLETE'
    elif not counts['qualified_entries'] and not counts['watch_research']:
        outcome='NO_ENTRY_UNDER_CURRENT_RULES'
    elif response.get('validation')=='NOT_CALLED':outcome='UNCHANGED_RESEARCH_DEFERRED'
    else:outcome='RESEARCH_COMPLETED_WITHOUT_BUY'
    result=dict(scope='OBSERVED_RESEARCH_POOL_ONLY_NOT_WHOLE_MARKET',outcome=outcome,
        counts=counts,entry_blockers=dict(reasons),discovery_status=(discovery or {}).get('status'),
        model_validation=response.get('validation'),
        candidate_search=response.get('candidate_search'),
        decision_warnings=dict(Counter(w for d in decision.get('decisions',[]) for w in d.get('warnings',[]))))
    if execution is not None:
        result['execution_status']=execution.get('status')
        result['broker_orders_sent']=sum(r.get('broker_order_sent') is True for r in execution.get('results',[]))
    return result
