"""Shared entry evidence checks; no outcomes, prediction or execution."""
from datetime import datetime
import json

from config import CODE_ROOT


def policy():
    result=json.loads((CODE_ROOT/'config/entry_decision.json').read_text(encoding='utf-8-sig'))
    for key in ('enabled','recovery_requires_completed_rebound','modeled_breakout_requires_observed_continuation'):
        if type(result.get(key)) is not bool:raise ValueError('INVALID_ENTRY_DECISION_POLICY:'+key)
    return result


def review(candidate,setup,bars,p):
    """A complete bar and a distinct earlier LIVE quote are different evidence.

    A positive 2R scenario is not a probability estimate. These hypotheses only
    require observable confirmation before an entry; they do not certify alpha.
    """
    last=float(candidate['last_price']);ma5=sum(float(b['close']) for b in bars[-5:])/5
    previous=bars[-1];blockers=[]
    facts=dict(last_completed_date=previous['date'],ma5=ma5,
        completed_rebound=previous['close']>previous['open'] and previous['close']>bars[-2]['close'],
        live_ma5_reclaimed=last>=ma5,
        target_distance_atr=(setup['target']-setup['entry'])/setup['atr14'],
        target_distance_per_holding_day_atr=(setup['target']-setup['entry'])/setup['atr14']/setup['holding_days'],
        target_probability=None)
    if setup['setup_family']=='MULTI_SESSION_RECOVERY' and p['recovery_requires_completed_rebound']:
        if not facts['completed_rebound']:blockers.append('COMPLETED_DAILY_REBOUND_NOT_OBSERVED')
        if not facts['live_ma5_reclaimed']:blockers.append('RECOVERY_MA5_NOT_RECLAIMED')
    if (setup['setup_family']=='FIRST_DAY_BREAKOUT' and
            setup['target_kind'] in ('MODELED_R_MULTIPLE_NOT_OBSERVED_PRICE','EMPIRICAL_HORIZON_RANGE_NOT_OBSERVED_PRICE',
                                    'REQUIRED_PAYOFF_NOT_OBSERVED_PRICE') and
            p['modeled_breakout_requires_observed_continuation']):
        at=datetime.fromisoformat(candidate['quote_timestamp']);history=[]
        level=setup['breakout_reference_price']
        for row in candidate.get('market_history',[]):
            if (row.get('symbol')!=candidate['symbol'] or row.get('quote_status')!='LIVE'
                    or row.get('is_trial') is not False or row.get('retrieval_mode')=='CACHE'):continue
            try:
                stamp=datetime.fromisoformat(row['exchange_time']);price=float(row['price'])
                if stamp.tzinfo is None or stamp.date()!=at.date() or not stamp<at or price<=0:continue
                history.append((stamp,price))
            except (KeyError,ValueError,TypeError):continue
        history.sort()
        prior=next(((stamp,price) for stamp,price in reversed(history) if price>level),None)
        continuation=bool(prior and last>=prior[1] and not any(
            stamp>prior[0] and price<=level for stamp,price in history))
        facts['breakout_continuation']=dict(confirmed=continuation,breakout_reference=level,
            confirmation_kind=('HELD_ABOVE_BREAKOUT_AND_NONDECLINING_DISTINCT_LIVE_OBSERVATION' if continuation else None),
            prior_quote_time=prior[0].isoformat() if prior else None,prior_price=prior[1] if prior else None)
        if not continuation:blockers.append('PRIOR_LIVE_BREAKOUT_CONTINUATION_NOT_OBSERVED')
    return dict(version=p['version'],approved=not blockers,blockers=blockers,facts=facts,
        scope='AS_OF_OBSERVATIONS_NOT_ESTIMATED_WIN_PROBABILITY')
