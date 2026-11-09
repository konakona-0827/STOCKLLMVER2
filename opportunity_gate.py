"""Cheap decision triggers; unchanged scans do not repeatedly purchase advice."""
from datetime import datetime
import json
import math
from pathlib import Path

from config import now


def snapshot(candidates, account, at):
    return dict(reviewed_at=at.isoformat(),cash=account.get('available_cash_cents'),
        budget_status=account.get('budget_status'),
        setups=sorted(s['setup_id'] for c in candidates for s in c.get('trade_setups',[])),
        plans={'|'.join((c['symbol'],s['strategy_id'],s['tier'],s['entry_basis'],s['target_kind'])):
               {key:s[key] for key in ('entry','target','invalidation','max_qty')}
               for c in candidates for s in c.get('trade_setups',[])},
        watches={c['symbol']:dict(trigger_kind=c['market_scout']['trigger_kind'],
            condition_price=c['market_scout']['condition_price'],
            expires_session=c['market_scout']['expires_session'],price=c['last_price'])
            for c in candidates if c.get('research_only') and
                (c.get('market_scout') or {}).get('status')=='RESEARCH_CONDITION_MET'},
        holdings={c['symbol']:dict(qty=int((c.get('paper_position') or {}).get('qty') or 0),
                                  price=c.get('bid') or c.get('last_price'),
                                  max_sell_qty=c.get('max_sell_qty'))
                  for c in candidates if int((c.get('paper_position') or {}).get('qty') or 0)>0})


def evaluate(trigger, candidates, account, path, policy, at=None):
    at=at or now()
    current=snapshot(candidates,account,at)
    result=dict(call_model=False,reason='NO_QUALIFIED_ENTRY_OR_POSITION',snapshot=current)
    if trigger=='MANUAL':
        return {**result,'call_model':True,'reason':'MANUAL_REVIEW'}
    if not current['setups'] and not current['holdings'] and not current['watches']:
        return result
    try:
        previous=json.loads(Path(path).read_text('utf-8'))
        if (not isinstance(previous,dict) or not isinstance(previous.get('plans',{}),dict)
                or not isinstance(previous.get('holdings',{}),dict)
                or not isinstance(previous.get('watches',{}),dict)):
            raise ValueError('invalid review state')
        for row in previous.get('plans',{}).values():
            if not isinstance(row,dict) or any(type(row.get(k)) not in (int,float) or
                    not math.isfinite(row[k]) or row[k]<=0 for k in ('entry','target','invalidation','max_qty')):
                raise ValueError('invalid plan state')
        for row in previous.get('holdings',{}).values():
            if not isinstance(row,dict) or type(row.get('qty')) is not int:
                raise ValueError('invalid holding state')
        for row in previous.get('watches',{}).values():
            if (not isinstance(row,dict) or any(type(row.get(k)) not in (int,float) or
                    not math.isfinite(row[k]) or row[k]<=0 for k in ('condition_price','price'))):
                raise ValueError('invalid watch state')
        previous_time=datetime.fromisoformat(previous['reviewed_at'])
        if previous_time.tzinfo is None:raise ValueError('missing timezone')
        age=(at-previous_time).total_seconds()
        if age<0:raise ValueError('future review')
    except (OSError,ValueError,TypeError,KeyError):
        return {**result,'call_model':True,'reason':'INITIAL_OR_UNAVAILABLE_REVIEW_STATE'}
    if previous.get('cycle_complete') is False:
        return {**result,'call_model':True,'reason':'PREVIOUS_CYCLE_NOT_HANDLED'}
    # Known stop/target touches always take precedence over cost suppression.
    for c in candidates:
        if c['symbol'] not in current['holdings']:continue
        price=current['holdings'][c['symbol']]['price']
        for lot in (c.get('buy_plans') or {}).get('lots',[]):
            plan=lot.get('plan') or {}
            if price is not None and ((plan.get('invalidation_price') is not None and price<=plan['invalidation_price'])
                                     or (plan.get('target_price') is not None and price>=plan['target_price'])):
                return {**result,'call_model':True,'reason':'ORIGINAL_EXIT_LEVEL_TOUCHED'}
    if (previous_time.date()!=at.date() or current['cash']!=previous.get('cash')
            or current['budget_status']!=previous.get('budget_status')):
        return {**result,'call_model':True,'reason':'NEW_SESSION_OR_ACCOUNT_CHANGE'}
    old_watches=previous.get('watches',{})
    for symbol,row in current['watches'].items():
        old=old_watches.get(symbol,{})
        if (any(row[k]!=old.get(k) for k in ('trigger_kind','condition_price','expires_session'))
                or not old.get('price') or
                abs(row['price']/old['price']-1)>=policy['entry_price_change_fraction']):
            return {**result,'call_model':True,'reason':'WATCH_RESEARCH_CONDITION_CHANGED'}
    old_plans=previous.get('plans',{})
    changed=set(current['plans'])!=set(old_plans)
    if not changed:
        for key,row in current['plans'].items():
            old=old_plans[key]
            if (row['max_qty']!=old.get('max_qty') or any(not old.get(field) or
                    abs(row[field]/old[field]-1)>=policy['entry_price_change_fraction']
                    for field in ('entry','target','invalidation'))):
                changed=True;break
    if changed:
        return {**result,'call_model':True,'reason':'ENTRY_OPPORTUNITY_CHANGED'}
    old_holdings=previous.get('holdings',{})
    if set(old_holdings)!=set(current['holdings']):
        return {**result,'call_model':True,'reason':'HOLDING_CHANGED'}
    for symbol,row in current['holdings'].items():
        old=old_holdings[symbol]
        if (row['qty']!=old.get('qty') or row.get('max_sell_qty')!=old.get('max_sell_qty')
                or row['price'] is None or not old.get('price')):
            return {**result,'call_model':True,'reason':'HOLDING_CHANGED_OR_UNKNOWN_PRICE'}
        if abs(row['price']/old['price']-1)>=policy['portfolio_price_change_fraction']:
            return {**result,'call_model':True,'reason':'POSITION_PRICE_CHANGED'}
    limit=(policy['unchanged_opportunity_review_seconds'] if current['setups'] or current['watches']
           else policy['max_portfolio_review_gap_seconds'])
    if age>=limit:
        return {**result,'call_model':True,'reason':'PERIODIC_REVIEW_DUE'}
    return {**result,'reason':'UNCHANGED_OPPORTUNITIES_AND_HOLDINGS'}


def record(path, candidates, account, at=None, *, cycle_complete=True, run_id=None, execution_status=None):
    path=Path(path);temporary=path.with_suffix('.tmp')
    state = dict(snapshot(candidates,account,at or now()), cycle_complete=cycle_complete,
                 run_id=run_id, execution_status=execution_status)
    temporary.write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8')
    temporary.replace(path)
