"""Frozen, unseen daily-price study; no broker, executable quotes or model calls.

freeze precedes outcome inspection. prepare uses past bars and today's opening
price only; score opens later price paths. Every stock-day is an independent
blank-account probe, not a portfolio equity curve or a claim of odd-lot fills.
"""
import argparse
from bisect import bisect_left
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from datetime import date,datetime,timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import types
from unittest.mock import patch

from operations.replay_archived_plans import load_daily

ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/'reports/target_reachability_20261111'
BASELINE='9fdcd2b'
CODE=['opportunity_engine.py','target_reachability.py','entry_decision.py','entry_structure.py',
      'pretrade_policy.py','trade_economics.py','daily_history.py','strategy_evidence.py',
      'trading_agent.py','token_budget.py','support_scout/trading.py','opportunity_gate.py',
      'opportunity_search_cycle.py','analysis_service.py','support_scout/market_sessions.py',
      'weekly_recovery.py','market_discovery.py','agent_decision.py','position_analyzer.py',
      'execution/auto_advice_executor.py','config/market_discovery.json','config/execution_live.json',
      'strategies/strategy_opportunity_v2.md','config/entry_decision.json',
      'config/target_reachability.json','config/opportunity_search.json',
      'operations/validate_target_reachability.py','operations/replay_archived_plans.py']

def read(path):return json.loads(Path(path).read_text('utf-8-sig'))
def save(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def git_file(name):return subprocess.check_output(['git','show',BASELINE+':'+name],cwd=ROOT)

def freeze():
    if (REPORT/'freeze.json').exists():raise ValueError('EXISTING_FREEZE_MUST_NOT_BE_REPLACED')
    source_paths=sorted((REPORT/'sources').glob('*.json'))+sorted((REPORT/'market_sessions').glob('*.json'))
    source_paths+=[REPORT/name for name in ('protocol.json','validation_universe.json',
                  'universe_source.json','verified_calendar.json','evaluation_plan.json')]
    save(REPORT/'freeze.json',dict(frozen_at=datetime.now(timezone.utc).isoformat(),baseline_commit=BASELINE,
        code={name:sha(ROOT/name) for name in CODE},
        sources={str(path.relative_to(REPORT)).replace('\\','/'):sha(path) for path in source_paths},
        baseline_sources={name:hashlib.sha256(git_file(name)).hexdigest() for name in
            ('opportunity_engine.py','config/opportunity_search.json','config/entry_decision.json')},
        outcomes_inspected=False,broker_access=False,executor_available=False,model_called=False))

def verified_freeze():
    frozen=read(REPORT/'freeze.json')
    for name,expected in frozen['code'].items():
        if sha(ROOT/name)!=expected:raise ValueError('FROZEN_CODE_CHANGED:'+name)
    for name,expected in frozen['sources'].items():
        if sha(REPORT/name)!=expected:raise ValueError('FROZEN_DATA_CHANGED:'+name)
    return frozen

def baseline_engine():
    source=git_file('opportunity_engine.py').decode('utf-8')
    guard="if candidate.get('quote_status') != 'LIVE' or candidate.get('is_trial'):"
    replacement="if (candidate.get('quote_status') != 'HISTORICAL_DAILY_PROXY' or candidate.get('execution_permitted') is not False or candidate.get('is_trial')):"
    if source.count(guard)!=1:raise ValueError('BASELINE_ADAPTER_GUARD_CHANGED')
    adapted=source.replace(guard,replacement)
    module=types.ModuleType('historical_proxy_baseline')
    exec(compile(adapted,'9fdcd2b:opportunity_engine.py:PROXY_GUARD_ONLY','exec'),module.__dict__)
    return module,dict(scope='RESEARCH_ONLY_GUARD_TRANSLATION_NO_FAKE_LIVE_QUOTES',
        original_guard=guard,research_guard=replacement,adapted_sha256=hashlib.sha256(adapted.encode()).hexdigest())

def calendar_context(dates):
    parsed=[date.fromisoformat(d) for d in dates]
    def previous(day):
        index=bisect_left(parsed,day)-1
        if index<0:raise ValueError('VERIFIED_CALENDAR_EXHAUSTED')
        return parsed[index]
    stack=ExitStack()
    for name in ('market_calendar.previous_trading_day','pretrade_policy.previous_trading_day',
                 'strategy_evidence.previous_trading_day'):
        stack.enter_context(patch(name,side_effect=previous))
    return stack

def candidate(symbol,name,rank,day,opening,bars,dates):
    from opportunity_engine import _round_price
    from pretrade_policy import price_levels
    past=[dict(b,complete=True,source='TWSE_DAILY') for b in bars if b['date']<day][-240:]
    previous=[d for d in dates if d<day][-240:]
    missing=sorted(set(previous[-21:])-{b['date'] for b in past})
    c=dict(symbol=symbol,name=name,asset_type='STOCK',last_price=opening,
        ask=float(_round_price(Decimal(str(opening))+Decimal('.01'),'STOCK',upward=True)),
        bid=float(_round_price(Decimal(str(opening))-Decimal('.01'),'STOCK')),
        open=opening,high=opening,low=opening,volume=0,
        quote_status='HISTORICAL_DAILY_PROXY',execution_permitted=False,is_trial=False,
        quote_timestamp=day+'T09:00:00+08:00',pricing_policy='PLANNED_LIMIT',quant_rank=rank,
        market_history=[],paper_position=dict(qty=0),position=dict(ai_managed_qty=0),
        max_buy_qty=999,max_sell_qty=0,daily_bars=past,
        daily_history_status=dict(market_calendar=dict(verified=True,source='TWSE_FMTQIK_ACTUAL_SESSIONS'),
            verified_trading_dates=previous,missing_20d_trading_dates=missing),
        eligible_strategies=['multi_day_trend'] if len(past)>=21 and not missing else [],
        research_eligibility='PAST_DAILY_TREND_ONLY_NO_INTRADAY_VOLUME_OR_CONFIRMATION_CLAIM',
        proxy_book_assumption='ONE_LEGAL_TICK_EACH_SIDE_OF_OFFICIAL_OPEN_NOT_OBSERVED_ODDLOT_BOOK')
    c['price_levels']=price_levels(c)
    # The ordinary-market opening observation is explicit, never relabeled LIVE.
    c['price_levels']+= [dict(id='today_'+key,price=opening,basis=day+':OFFICIAL_OPEN_PROXY')
                        for key in ('open','high','low')]
    return c

def prepare():
    from opportunity_engine import select,policy
    from execution.auto_advice_executor import LiveExecutionConfig
    from target_reachability import view
    verified_freeze()
    if (REPORT/'probes.json').exists():raise ValueError('EXISTING_PROBES_MUST_NOT_BE_REPLACED')
    cal=read(REPORT/'verified_calendar.json')
    if not cal['calendar']['verified']:raise ValueError('CALENDAR_UNVERIFIED')
    dates=cal['dates'];universe=read(REPORT/'validation_universe.json')['selections']
    data={c['symbol']:load_daily(REPORT/'sources',c['symbol']) for c in universe}
    old,adapter=baseline_engine()
    old_policy=json.loads(git_file('config/opportunity_search.json'))
    old_entry=json.loads(git_file('config/entry_decision.json'))
    rules=LiveExecutionConfig(minimum_commission_twd=Decimal(1),max_order_twd=Decimal(100000))
    account=dict(available_cash_cents=1000000,available_cash_twd=10000,
                 capital_limit_twd=10000,budget_status='READY')
    rows=[]
    for day in (d for d in dates if '2026-01-05'<=d<='2026-03-31'):
        pool=[];missing=[]
        with calendar_context(dates):
            for rank,item in enumerate(universe,1):
                symbol=item['symbol'];today=next((b for b in data[symbol] if b['date']==day),None)
                if not today:
                    missing.append(dict(day=day,symbol=symbol,status='MISSING_OPENING_OBSERVATION'));continue
                pool.append(candidate(symbol,item['name'],rank,day,today['open'],data[symbol],dates))
            before=deepcopy(pool);after=deepcopy(pool)
            with patch('entry_decision.policy',return_value=old_entry):
                _,old_audit=old.select(before,account,rules,old_policy)
            _,new_audit=select(after,account,rules,policy(),research_proxy=True)
        for a,b in zip(before,after):
            rows.append(dict(day=day,symbol=a['symbol'],opening=a['open'],quote_status=a['quote_status'],
                execution_permitted=False,past_bar_count=len(a['daily_bars']),
                past_bars_sha256=hashlib.sha256(json.dumps(a['daily_bars'],sort_keys=True).encode()).hexdigest(),
                before=dict(plans=a['trade_setups'],status=a['opportunity_status'],
                            selected=a['symbol'] in old_audit['research_symbols']),
                after=dict(plans=b['trade_setups'],status=b['opportunity_status'],
                           pending=b['pending_entry_setups'],reachability=view(b.get('target_reachability') or {}),
                           selected=b['symbol'] in new_audit['research_symbols'])))
        rows+=missing
    save(REPORT/'probes.json',dict(freeze_sha256=sha(REPORT/'freeze.json'),adapter=adapter,account=account,
        protocol='INDEPENDENT_STOCK_DAY_NEXT_OPEN_PROXY_NO_PORTFOLIO_SIMULATION',
        expected_stock_days=len([d for d in dates if '2026-01-05'<=d<='2026-03-31'])*len(universe),rows=rows))
    print('prepared',len(rows),'stock-days; outcome scoring has not run')

def path_outcome(day,setup,bars,dates):
    target,stop=setup['target'],setup['invalidation']
    sessions=[d for d in dates if d>day][:setup['holding_days']]
    if len(sessions)!=setup['holding_days']:return dict(status='UNAVAILABLE',reason='CALENDAR_HORIZON_INCOMPLETE')
    lookup={b['date']:b for b in bars}
    # Odd lots acquired today are not assumed sellable in today's session.
    for session in sessions:
        b=lookup.get(session)
        if b is None:return dict(status='UNAVAILABLE',reason='MISSING_REQUIRED_SESSION',at=session)
        if b['open']<=stop:status,reason='FAILURE','OPEN_AT_OR_BELOW_STOP'
        elif b['open']>=target:status,reason='SUCCESS','OPEN_AT_OR_ABOVE_TARGET'
        elif b['low']<=stop and b['high']>=target:status,reason='AMBIGUOUS','BOTH_BARRIERS_NO_INTRADAY_ORDER'
        elif b['low']<=stop:status,reason='FAILURE','STOP_BEFORE_TARGET'
        elif b['high']>=target:status,reason='SUCCESS','TARGET_BEFORE_STOP'
        else:continue
        return dict(status=status,reason=reason,at=session)
    return dict(status='FAILURE',reason='TARGET_NOT_REACHED_WITHIN_HORIZON',at=sessions[-1])

def score():
    verified_freeze();probes=read(REPORT/'probes.json')
    if probes['freeze_sha256']!=sha(REPORT/'freeze.json'):raise ValueError('PROBES_NOT_FROM_FREEZE')
    if (REPORT/'validation.json').exists():raise ValueError('EXISTING_SCORES_MUST_NOT_BE_REPLACED')
    dates=read(REPORT/'verified_calendar.json')['dates']
    data={c['symbol']:load_daily(REPORT/'sources',c['symbol'])
          for c in read(REPORT/'validation_universe.json')['selections']}
    result=[]
    for probe in probes['rows']:
        item=dict(day=probe['day'],symbol=probe['symbol'])
        if probe.get('status'):
            result.append(dict(item,status=probe['status']));continue
        for version in ('before','after'):
            choices=[]
            for setup in probe[version]['plans']:
                entry_supported=setup['entry_basis']=='CURRENT_ASK'
                choices.append(dict(setup=setup,entry_status='OPENING_BOOK_PROXY' if entry_supported else 'UNCONFIRMED_LIMIT_ENTRY',
                    path=path_outcome(probe['day'],setup,data[probe['symbol']],dates) if entry_supported else
                        dict(status='UNCONFIRMED_ENTRY',reason='NO_POST_DECISION_ODDLOT_ASK_AT_LIMIT')))
            executable=[c for c in choices if c['entry_status']=='OPENING_BOOK_PROXY']
            item[version]=dict(plans=choices,chosen=executable[0] if executable else None,
                selected=probe[version]['selected'])
        # Fixed baseline opportunities remain in the denominator even when new
        # screens reject them. Never relabel their truth using a different plan.
        item['reference_positive']=any(c['path']['status']=='SUCCESS' for c in item['before']['plans'])
        item['reference_failure']=bool(item['before']['chosen'] and item['before']['chosen']['path']['status']=='FAILURE')
        item['new_success']=bool(item['after']['chosen'] and item['after']['chosen']['path']['status']=='SUCCESS')
        result.append(item)
    valid=[r for r in result if 'before' in r]
    # A second, fixed baseline cohort removes overlapping same-symbol horizons.
    # Its membership is decided by signal time, never by the later outcome.
    disjoint=[];blocked_until={}
    for row in valid:
        chosen=row['before']['chosen']
        if not chosen or row['day']<=blocked_until.get(row['symbol'],''):continue
        disjoint.append(row)
        horizon=[d for d in dates if d>row['day']][:chosen['setup']['holding_days']]
        blocked_until[row['symbol']]=horizon[-1]
    summaries={}
    for version in ('before','after'):
        chosen=[r[version]['chosen'] for r in valid if r[version]['chosen']]
        top=[r[version]['chosen'] for r in valid if r[version]['selected'] and r[version]['chosen']]
        summaries[version]=dict(qualified_stock_days=sum(bool(r[version]['plans']) for r in valid),
            opening_proxy_plans=len(chosen),outcomes=dict(Counter(c['path']['status'] for c in chosen)),
            selected_top3_opening_plans=len(top),top3_outcomes=dict(Counter(c['path']['status'] for c in top)),
            unconfirmed_limit_plans=sum(c['entry_status']=='UNCONFIRMED_LIMIT_ENTRY' for r in valid for c in r[version]['plans']))
    summary=dict(scope='UNSEEN_STOCKS_AND_DATES_DAILY_PRICE_PROXY_NOT_ODDLOT_EXECUTION_OR_AGENT_BACKTEST',
        freeze_sha256=sha(REPORT/'freeze.json'),probes_sha256=sha(REPORT/'probes.json'),
        expected_stock_days=probes['expected_stock_days'],observed_stock_days=len(valid),
        missing_stock_days=len(result)-len(valid),comparison=summaries,
        baseline_reference_positives=sum(r['reference_positive'] for r in valid),
        reference_positives_retained_as_new_success=sum(r['reference_positive'] and r['new_success'] for r in valid),
        reference_positives_missed=sum(r['reference_positive'] and not r['new_success'] for r in valid),
        baseline_failures_without_new_plan=sum(r['reference_failure'] and not r['after']['chosen'] for r in valid),
        new_successes_outside_baseline_positive=sum(not r['reference_positive'] and r['new_success'] for r in valid),
        disjoint_baseline_cohort=dict(stock_days=len(disjoint),
            original_outcomes=dict(Counter(r['before']['chosen']['path']['status'] for r in disjoint)),
            new_outcomes=dict(Counter(r['after']['chosen']['path']['status'] if r['after']['chosen'] else 'NO_PLAN' for r in disjoint)),
            reference_positives=sum(r['reference_positive'] for r in disjoint),
            positives_retained=sum(r['reference_positive'] and r['new_success'] for r in disjoint),
            positives_missed=sum(r['reference_positive'] and not r['new_success'] for r in disjoint),
            event_ids=[r['day']+'|'+r['symbol'] for r in disjoint]),
        symbols_with_new_success=sorted({r['symbol'] for r in valid if r['new_success']}),
        all_wait=not summaries['after']['opening_proxy_plans'],model_called=False,orders_sent=False,
        limitations=['Daily ordinary-market paths and an assumed one-tick book do not verify odd-lot execution.',
            'Only opening observations are available; intraday recovery and first-day continuation cannot be scored.',
            'Stock-days are independent probes with an unchanged initial account; repeated signals are not independent trades.',
            'Raw prices are not corporate-action adjusted; this is target-path evidence, not portfolio returns.',
            'Twenty deterministic new names are not a whole-market opportunity denominator.',
            'A baseline-positive/new-failure remains a miss even if the new target was farther away.'],rows=result)
    save(REPORT/'validation.json',summary)
    print(json.dumps({k:v for k,v in summary.items() if k not in ('rows','limitations')},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('freeze','prepare','score'))
    args=parser.parse_args();globals()[args.action]()
