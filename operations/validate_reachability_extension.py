"""Prospective extension of the inconclusive first study; financial code stays frozen.

Uses the same twenty names, opening-only features, costs, entry assumptions and
labels. Only the new decision dates (May-September) are extended before download
and scoring. The original January-March result is retained, including failures.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
from unittest.mock import patch

from operations import validate_target_reachability as study
from operations import collect_reachability_history as collector
from operations.replay_archived_plans import load_daily

PRIMARY=study.REPORT
EXT=PRIMARY/'extension'
START,END='2026-05-04','2026-09-30'

def parent_verified():
    study.REPORT=PRIMARY
    parent=study.verified_freeze()
    study.REPORT=EXT
    if (EXT/'freeze.json').exists():
        frozen=study.read(EXT/'freeze.json')
        if frozen['parent_freeze_sha256']!=study.sha(PRIMARY/'freeze.json'):
            raise ValueError('PARENT_FREEZE_CHANGED')
        if frozen['extension_program_sha256']!=study.sha(Path(__file__)):
            raise ValueError('EXTENSION_PROGRAM_CHANGED')
    return parent

def collect():
    parent_verified()
    EXT.mkdir(parents=True,exist_ok=True)
    if (EXT/'freeze.json').exists():raise ValueError('ALREADY_FROZEN')
    protocol=study.read(PRIMARY/'protocol.json')
    protocol['validation']['entry_dates']=[START,END]
    protocol['validation']['outcomes_until']='2026-10-30'
    protocol['extension_reason']='FIRST_COHORT_ZERO_BASELINE_POSITIVES_CANNOT_VALIDATE_OPPORTUNITY_RECALL'
    protocol['financial_code_changed_after_first_cohort']=False
    study.save(EXT/'protocol.json',protocol)
    plan=study.read(PRIMARY/'evaluation_plan.json')
    plan['decision']=START+' through '+END+', same fixed20 names and same frozen financial rules'
    plan['prior_result']='Original 1080 stock-days: baseline4 failures, revised3 failures, zero reference positives; retained unchanged'
    plan['extension_locked_before_collection_and_scoring']=True
    study.save(EXT/'evaluation_plan.json',plan)
    for name in ('validation_universe.json','universe_source.json'):
        shutil.copy2(PRIMARY/name,EXT/name)
    collector.REPORT=EXT
    universe=study.read(EXT/'validation_universe.json')['selections']
    jobs=[(item['symbol'],f'2026{month:02d}') for item in universe for month in range(5,11)]
    completed=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(collector.fetch,*job) for job in jobs]
        for future in as_completed(futures):
            completed.append(future.result())
            if len(completed)%30==0:print('collected',len(completed),'of',len(jobs),flush=True)
    study.save(EXT/'collection_manifest.json',dict(jobs=len(jobs),results=completed))
    from support_scout.market_sessions import load_sessions
    dates,calendar=load_sessions('2026-10-30',460,EXT/'market_sessions')
    study.save(EXT/'verified_calendar.json',dict(dates=dates,calendar=calendar))
    print('calendar',len(dates),calendar['verified'],flush=True)

def freeze():
    parent_verified()
    study.freeze()
    frozen=study.read(EXT/'freeze.json')
    frozen.update(parent_freeze_sha256=study.sha(PRIMARY/'freeze.json'),
                  primary_validation_sha256=study.sha(PRIMARY/'validation.json'),
                  extension_program_sha256=study.sha(Path(__file__)),
                  unchanged_financial_source_hashes=True)
    study.save(EXT/'freeze.json',frozen)

def combined(symbol):
    rows=load_daily(PRIMARY/'sources',symbol)+load_daily(EXT/'sources',symbol)
    if len({r['date'] for r in rows})!=len(rows):raise ValueError('OVERLAPPING_SOURCE_DATES')
    return sorted(rows,key=lambda row:row['date'])

def prepare():
    from execution.auto_advice_executor import LiveExecutionConfig
    from opportunity_engine import select,policy
    from target_reachability import view
    parent_verified();study.verified_freeze()
    if (EXT/'probes.json').exists():raise ValueError('EXISTING_PROBES_MUST_NOT_BE_REPLACED')
    cal=study.read(EXT/'verified_calendar.json')
    if not cal['calendar']['verified']:raise ValueError('CALENDAR_UNVERIFIED')
    dates=cal['dates'];universe=study.read(EXT/'validation_universe.json')['selections']
    data={c['symbol']:combined(c['symbol']) for c in universe}
    old,adapter=study.baseline_engine()
    old_policy=json.loads(study.git_file('config/opportunity_search.json'))
    old_entry=json.loads(study.git_file('config/entry_decision.json'))
    rules=LiveExecutionConfig(minimum_commission_twd=Decimal(1),max_order_twd=Decimal(100000))
    account=dict(available_cash_cents=1000000,available_cash_twd=10000,
                 capital_limit_twd=10000,budget_status='READY')
    rows=[]
    days=[d for d in dates if START<=d<=END]
    for day in days:
        pool=[];missing=[]
        with study.calendar_context(dates):
            for rank,item in enumerate(universe,1):
                symbol=item['symbol'];today=next((b for b in data[symbol] if b['date']==day),None)
                if not today:
                    missing.append(dict(day=day,symbol=symbol,status='MISSING_OPENING_OBSERVATION'));continue
                pool.append(study.candidate(symbol,item['name'],rank,day,today['open'],data[symbol],dates))
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
    study.save(EXT/'probes.json',dict(freeze_sha256=study.sha(EXT/'freeze.json'),adapter=adapter,account=account,
        protocol='SAME_FROZEN_FINANCIAL_RULES_NEW_DATES_INDEPENDENT_STOCK_DAY_NEXT_OPEN_PROXY',
        expected_stock_days=len(days)*len(universe),rows=rows))
    print('prepared',len(rows),'new stock-days; outcomes not scored',flush=True)

def score():
    parent_verified()
    with patch.object(study,'load_daily',side_effect=lambda source_dir,symbol:combined(symbol)):
        study.score()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('collect','freeze','prepare','score'))
    args=parser.parse_args();globals()[args.action]()
