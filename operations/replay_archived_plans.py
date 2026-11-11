"""Reproduce archived plan outcomes with explicit daily-price proxy limitations."""
import argparse
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from market_calendar import twse_closed_reason


def sessions_after(day, count):
    day=date.fromisoformat(day);sessions=[]
    while len(sessions)<count:
        day+=timedelta(days=1)
        if twse_closed_reason(day) is None:sessions.append(day.isoformat())
    return sessions


def parse_twse(record):
    payload=record['payload']
    required=['日期','成交股數','成交金額','開盤價','最高價','最低價','收盤價']
    if payload.get('stat')!='OK' or payload.get('fields',[])[:7]!=required:
        raise ValueError('TWSE_DAILY_SCHEMA_CHANGED')
    bars=[]
    for row in payload['data']:
        y,m,d=map(int,row[0].split('/'));y=y+1911 if y<1911 else y
        number=lambda value:float(str(value).replace(',',''))
        if all(str(v).strip() in ('--','') for v in row[3:7]):
            # Official rows can lack all OHLC values even with reported volume.
            # Do not invent prices; retain missing sessions against the calendar.
            continue
        op,hi,lo,close=map(number,row[3:7])
        if not 0<lo<=min(op,close)<=max(op,close)<=hi:raise ValueError('INVALID_DAILY_OHLC')
        bars.append(dict(date=date(y,m,d).isoformat(),open=op,high=hi,low=lo,close=close,
                         volume=number(row[1]),turnover=number(row[2]),source_url=record['source_url']))
    return bars


def daily_plan_outcome(entry_day, target, stop, holding_days, bars):
    """Price-path test, not an assumption that a daily high is an odd-lot fill.

    Ignore the entry day's range: its extrema may predate the decision and new
    odd lots cannot be sold that day. The horizon counts completed later sessions,
    matching trade_plan_context.plan_review_status().
    """
    if (type(holding_days) is not int or holding_days<1 or not 0<stop<target):
        raise ValueError('INVALID_PLAN')
    sessions=sessions_after(entry_day,holding_days)
    lookup={bar['date']:bar for bar in bars}
    for day in sessions:
        if day not in lookup:
            return dict(status='UNAVAILABLE',reason='MISSING_REQUIRED_SESSION',at=day,deadline=sessions[-1])
        b=lookup[day]
        if b['open']<=stop:
            status,reason='FAILURE','OPEN_AT_OR_BELOW_INVALIDATION'
        elif b['open']>=target:
            status,reason='SUCCESS','OPEN_AT_OR_ABOVE_TARGET'
        elif b['low']<=stop and b['high']>=target:
            status,reason='AMBIGUOUS','BOTH_BARRIERS_TOUCHED_WITHOUT_INTRADAY_ORDER'
        elif b['low']<=stop:
            status,reason='FAILURE','INVALIDATION_BEFORE_TARGET'
        elif b['high']>=target:
            status,reason='SUCCESS','TARGET_BEFORE_INVALIDATION'
        else:continue
        return dict(status=status,reason=reason,at=day,deadline=sessions[-1],evidence=b,
                    execution_scope='REGULAR_MARKET_DAILY_PRICE_PROXY_NOT_VERIFIED_ODDLOT_EXIT')
    return dict(status='FAILURE',reason='TARGET_NOT_REACHED_WITHIN_ORIGINAL_HORIZON',
                at=sessions[-1],deadline=sessions[-1],evidence=lookup[sessions[-1]],
                execution_scope='REGULAR_MARKET_DAILY_PRICE_PROXY_NOT_VERIFIED_ODDLOT_EXIT')


def find_quote_entry(candidate, setup, quotes):
    """Only a later, same-day, non-trial LIVE ask can support a ROD limit entry."""
    at=datetime.fromisoformat(candidate['quote_timestamp']);limit=setup['entry']
    if at.tzinfo is None or not 0<setup['invalidation']<limit:raise ValueError('INVALID_ENTRY_SNAPSHOT')
    if (candidate.get('quote_status')=='LIVE' and not candidate.get('is_trial')
            and candidate.get('ask') is not None and setup['invalidation']<candidate['ask']<=limit):
        return dict(status='QUOTE_SUPPORTED',at=at.isoformat(),price=candidate['ask'],
                    actual_fill_verified=False,depth_verified=False)
    for q in sorted(quotes,key=lambda row:row.get('quote_timestamp') or ''):
        raw=q.get('quote_timestamp')
        if not raw:continue
        stamp=datetime.fromisoformat(raw)
        if (stamp<=at or stamp.date()!=at.date() or q.get('quote_status')!='LIVE' or q.get('is_trial')
                or q.get('ask') is None):continue
        if setup['invalidation']<q['ask']<=limit and (q.get('last_price') or q['ask'])>setup['invalidation']:
            return dict(status='QUOTE_SUPPORTED',at=stamp.isoformat(),price=q['ask'],
                        actual_fill_verified=False,depth_verified=False)
    return dict(status='UNCONFIRMED_ENTRY',reason='NO_POST_DECISION_SAME_DAY_ASK_AT_LIMIT',
                actual_fill_verified=False,depth_verified=False)


def load_daily(source_dir, symbol):
    result={}
    for path in sorted(Path(source_dir).glob('twse_'+symbol+'_*.json')):
        if path.name.endswith('.failure.json'):
            # Explicit collection failures remain in the manifest. They are
            # missing observations, not malformed price rows or zero prices.
            continue
        for bar in parse_twse(json.loads(path.read_text(encoding='utf-8'))):
            if bar['date'] in result and result[bar['date']]!=bar:raise ValueError('CONFLICTING_DAILY_BARS')
            result[bar['date']]=bar
    return [result[day] for day in sorted(result)]


def build_report(archive, audit_path, candidates_path, output):
    archive=Path(archive);output=Path(output)
    audited=json.loads(Path(audit_path).read_text(encoding='utf-8'))
    reviews=audited['reviews'];closed=audited['closed_cycles']
    old_plans=[]
    for row in reviews:
        d=row.get('decision') or {}
        if row['side']!='BUY' or not d.get('target_price') or not row.get('filled_quantity'):continue
        # The archived original analysis timestamp is already Taipei-local.
        original=json.loads((archive/'data/analysis/runs'/row['run_id']/'run.json').read_text(encoding='utf-8-sig'))
        day=original['started_at'][:10]
        outcome=daily_plan_outcome(day,d['target_price'],d['invalidation_price'],d['holding_days'],
                                   load_daily(output/'sources',row['symbol']))
        old_plans.append(dict(event_id=row['run_id']+'|'+row['symbol'],symbol=row['symbol'],entry_date=day,
            entry_price=row['price_evidence']['price'],entry_evidence=row['price_evidence'],
            quantity=row['filled_quantity'],target=d['target_price'],invalidation=d['invalidation_price'],
            holding_days=d['holding_days'],price_path_outcome=outcome))
    scenarios={}
    for fee in ('1','20'):
        profits=[Decimal(row['scenarios'][fee]) for row in closed if row['prices_verified']]
        scenarios[fee]=dict(closed_trades=len(profits),profitable=sum(p>0 for p in profits),
                            net_win_rate=sum(p>0 for p in profits)/len(profits) if profits else None,
                            assumed_net_pnl_twd=str(sum(profits)),actual_fees_verified=False)
    quotes={}
    for folder in sorted((archive/'data/analysis/runs').glob('*')):
        path=folder/'multi_market_snapshot.json'
        if not path.exists():continue
        for q in json.loads(path.read_text(encoding='utf-8-sig')).get('quotes',[]):
            quotes.setdefault(q['symbol'],[]).append(q)
    candidates=json.loads(Path(candidates_path).read_text(encoding='utf-8'))
    seen=set();current=[]
    for row in candidates:
        c=row['candidate'];day=c['quote_timestamp'][:10];key=(day,c['symbol'])
        if key in seen:continue
        seen.add(key);plans=[]
        for setup in c['trade_setups']:
            entry=find_quote_entry(c,setup,quotes.get(c['symbol'],[]))
            path=daily_plan_outcome(day,setup['target'],setup['invalidation'],setup['holding_days'],
                                    load_daily(output/'sources',c['symbol']))
            status=path['status'] if entry['status']=='QUOTE_SUPPORTED' else 'UNAVAILABLE'
            plans.append(dict(setup=setup,entry_evidence=entry,price_path_outcome=path,
                              quote_supported_proxy_status=status))
        current.append(dict(symbol=c['symbol'],source_run=row['run'],as_of=c['quote_timestamp'],
                            old_decision=(row.get('old_decision') or {}).get('decision'),plans=plans))
    actual=json.loads((output/'current_agent/result.json').read_text(encoding='utf-8'))
    decisions={row['symbol']:row for row in actual['decision']['decisions']}
    labelled=[];unconfirmed=[]
    for row in current:
        d=decisions.get(row['symbol'],{});row['current_agent_decision']=d.get('decision');row['current_agent_reason']=d.get('reason')
        if actual['response']['validation']!='VALID':
            unconfirmed.append(row['symbol']);row['classification']='MODEL_ERROR';continue
        # Credit a BUY only against its selected plan; WAIT considers all known
        # valid alternatives, so declining a different profitable path is an FN.
        options=[p for p in row['plans'] if p['quote_supported_proxy_status'] in ('SUCCESS','FAILURE')]
        if d.get('decision')=='BUY':
            options=[p for p in options if p['setup']['entry']==d['suggested_price'] and
                     p['setup']['target']==d['target_price'] and p['setup']['invalidation']==d['invalidation_price']]
        positives=[p for p in options if p['quote_supported_proxy_status']=='SUCCESS']
        unknown=[p for p in row['plans'] if p['quote_supported_proxy_status']=='UNAVAILABLE']
        if d.get('decision')!='BUY' and not positives and unknown:
            unconfirmed.append(row['symbol']);row['classification']='UNAVAILABLE';continue
        if not options:unconfirmed.append(row['symbol']);row['classification']='UNAVAILABLE';continue
        success=bool(positives);picked=d.get('decision')=='BUY'
        classification=('TP' if picked else 'FN') if success else ('FP' if picked else 'TN')
        row['classification']=classification;labelled.append(classification)
    counts=Counter(labelled);tp,fp,fn=(counts[k] for k in ('TP','FP','FN'))
    archive_buys=[]
    for path in sorted((archive/'data/analysis/runs').glob('*/selection_decision.json')):
        archive_buys.extend(d for d in json.loads(path.read_text(encoding='utf-8-sig')).get('decisions',[])
                            if d.get('decision')=='BUY')
    report=dict(scope='OCTOBER_ARCHIVE_AND_ONE_REAL_CURRENT_AGENT_FROZEN_REPLAY',
        archive_buy_coverage=dict(buy_decisions=len(archive_buys),structured_verified_fill_plans=len(old_plans),
            missing_structured_plan_decisions=sum(not all(d.get(k) is not None for k in
                ('target_price','invalidation_price','holding_days')) for d in archive_buys)),
        original_model_counts=audited['all_trades_model_counts'],realized_trade_cost_scenarios=scenarios,
        original_structured_plans=old_plans,
        original_daily_price_outcomes=dict(Counter(p['price_path_outcome']['status'] for p in old_plans)),
        current_rule_cases=current,current_agent=dict(validation=actual['response']['validation'],
            model=actual['response']['api'].get('model'),frozen_candidate_count=len(current),
            broker_access=False,orders_sent=False,usage=actual['response']['api'].get('usage'),
            pretrade_usage=(actual['response'].get('pretrade_review') or {}).get('api',{}).get('usage'),
            classifications=dict(counts),unconfirmed_symbols=unconfirmed,
            quote_supported_proxy_precision=tp/(tp+fp) if tp+fp else None,
            quote_supported_proxy_recall=tp/(tp+fn) if tp+fn else None,
            confirmed_misses=fn,whole_market_recall=None),
        provenance=dict(audit_results_sha256=hashlib.sha256(Path(audit_path).read_bytes()).hexdigest(),
            frozen_candidates_sha256=hashlib.sha256(Path(candidates_path).read_bytes()).hexdigest()),
        limitations=['Daily outcomes are regular-market price-path proxies, not verified odd-lot exit fills.',
            'A quoted ask does not verify queue priority, depth or actual counterfactual fills.',
            'Unconfirmed limit entries are not credited as either a confirmed success or a confirmed miss.',
            'The current Agent received only frozen original inputs; external search and broker access were disabled.',
            'Historical model training knowledge, latency, repricing and alternative portfolio paths are not controlled.',
            'Two candidates from one day cannot establish current live performance or whole-market recall.'])
    (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive-root',type=Path,required=True)
    p.add_argument('--audit-results',type=Path,required=True)
    p.add_argument('--candidates',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();r=build_report(args.archive_root,args.audit_results,args.candidates,args.output)
    print(json.dumps(dict(original_outcomes=r['original_daily_price_outcomes'],
                          realized=r['realized_trade_cost_scenarios'],current_agent=r['current_agent']),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
