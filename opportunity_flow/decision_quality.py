"""Evidence coverage diagnostics, separate from trading permission and P&L."""
from collections import Counter

KNOWN_EVENT_RISKS=frozenset(('UPCOMING_CORPORATE_ACTION_REQUIRES_NEW_PLAN','MATERIAL_TRADING_OR_CAPITAL_EVENT'))


def summarize(candidates,calls):
    states=Counter(c['state'] for c in candidates)
    gaps=Counter();routes_far=0;reviewed=0
    for c in candidates:
        availability=c.get('information_availability') or {}
        for name,info in availability.items():
            if not isinstance(info,dict):continue
            origin=info.get('origin','')
            if origin.startswith(('SYSTEM','SOURCE','PROVIDER')):gaps[name+':'+origin]+=1
        review=c.get('review') or {}
        if all(review.get(role) for role in ('researcher','challenger')):reviewed+=1
        evidence={e['evidence_id']:e.get('facts') for e in (c.get('packet') or {}).get('evidence',[]) if 'evidence_id' in e}
        f=evidence.get('E_FEATURES') or {};p75=f.get('historical_high_excursion_p75')
        if isinstance(p75,(int,float)):
            routes_far+=sum(r.get('eligible') is True and r.get('target_required_return',0)>p75
                for r in evidence.get('E_ECONOMICS') or [])
    pending=sum(states.get(s,0) for s in ('REVIEW_PENDING','REVIEW_IN_FLIGHT'))
    failed=sum(states.get(s,0) for s in ('REVIEW_ERROR','REVIEW_ERROR_BACKOFF','REVIEW_SERVICE_BACKOFF','REVIEW_DEFERRED_SERVICE_ERROR'))
    deferred=sum(n for s,n in states.items() if s.startswith('REVIEW_DEFERRED'))
    return dict(schema_version=1,scope='THIS_CYCLE_OBSERVED_INPUTS_ONLY',symbols_observed=len(candidates),
        state_counts=dict(states),paired_research_candidates=reviewed,research_pending=pending,
        research_errors_or_backoff=failed,deferred_candidates=deferred,
        data_blocked=states.get('DATA_BLOCKED',0),information_gaps=dict(gaps),
        model_call_errors=sum(c.get('status')=='ERROR' for c in calls),
        eligible_routes_above_descriptive_historical_p75=routes_far,
        market_opportunity_absence_verified=False,
        limitations=['NO_ALL_MARKET_OPPORTUNITY_OR_PROFITABILITY_CLAIM',
            'DATA_BLOCKED_AND_SERVICE_ERRORS_ARE_NOT_MARKET_WATCH_DECISIONS',
            'HISTORICAL_OVERLAPPING_EXCURSIONS_ARE_NOT_CALIBRATED_PROBABILITIES',
            'NO_COUNTERFACTUAL_FILL_OR_RETURN_WITHOUT_FROZEN_QUOTE_PATH'])
