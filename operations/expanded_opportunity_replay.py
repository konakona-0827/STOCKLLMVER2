"""Frozen October benchmark and paired SDK replay; no broker or executor.

Candidates and reference routes are frozen before outcomes. Revised filters do
not change the opportunity denominator. Independent snapshots are not a portfolio.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, data):
    import tempfile
    import time
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,
            prefix=path.name+'.',suffix='.tmp',delete=False) as stream:
        stream.write(json.dumps(data,ensure_ascii=False,indent=2)+'\n');temp=Path(stream.name)
    try:
        for attempt in range(40):
            try:
                temp.replace(path);break
            except PermissionError:
                # Windows readers/scanners may briefly hold the destination
                # without delete-sharing. The temporary file remains unique.
                if attempt==39:raise
                time.sleep(.01)
    finally:
        if temp.exists():temp.unlink()


def digest(data):
    return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def route_outcome(candidate,setup,quotes,bars):
    from operations.replay_archived_plans import find_quote_entry,daily_plan_outcome
    entry=find_quote_entry(candidate,setup,quotes)
    day=candidate['quote_timestamp'][:10]
    outcome=daily_plan_outcome(day,setup['target'],setup['invalidation'],setup['holding_days'],bars)
    if entry['status']=='QUOTE_SUPPORTED':
        touched=[q for q in quotes if q.get('quote_timestamp') and q['quote_timestamp']>entry['at']
            and q['quote_timestamp'][:10]==entry['at'][:10] and q.get('quote_status')=='LIVE'
            and not q.get('is_trial') and q.get('last_price') is not None
            and q['last_price']<=setup['invalidation']]
        if touched:
            outcome=dict(status='FAILURE',reason='POST_ENTRY_SAME_DAY_INVALIDATION',
                at=min(q['quote_timestamp'] for q in touched),execution_scope='ARCHIVED_ODDLOT_QUOTE_PRICE_PROXY')
        elif outcome['status']=='SUCCESS' and any(b['date']==day and b['low']<=setup['invalidation'] for b in bars):
            outcome=dict(status='AMBIGUOUS',reason='ENTRY_DAY_INVALIDATION_ORDER_UNKNOWN',
                execution_scope='DAILY_RANGE_CANNOT_ESTABLISH_POST_ENTRY_ORDER')
    return dict(entry=entry,daily_price_outcome=outcome,
        status=outcome['status'] if entry['status']=='QUOTE_SUPPORTED' else 'UNAVAILABLE')


def prepare(archive, output):
    from analysis_service import apply_buy_limits
    from execution.auto_advice_executor import LiveExecutionConfig
    from opportunity_engine import refresh, policy
    from pretrade_policy import price_levels
    from quant_scanner import scan_and_rank
    from strategy_evidence import assess
    from universe import load_rules
    from config import CODE_ROOT,TAIPEI
    from scheduler import twse_oddlot_first_match
    from operations.replay_archived_plans import load_daily
    archive=Path(archive);output=Path(output);protocol=read(output/'protocol.json')
    history=defaultdict(dict);revisions=[]
    for path in sorted((archive/'data/analysis/daily_history').glob('*.json')):
        x=read(path)
        for b in x['bars']:history[x['symbol']][b['date']]=b
    for symbol in protocol['universe']:
        for b in load_daily(output/'sources',symbol):
            if b['date'] in history[symbol]:
                old=history[symbol][b['date']]
                if any(abs(float(old[k])-b[k])>1e-8 for k in ('open','high','low','close','volume')):
                    revisions.append(dict(symbol=symbol,date=b['date']))
            # Completed public data are reconstructed now, not claimed to have
            # been fetched by the old agent at the original decision time.
            history[symbol][b['date']]=dict(b,complete=True,source='TWSE_DAILY')
    account=dict(capital_limit_twd=protocol['capital_twd'],available_cash_twd=protocol['capital_twd'],
                 available_cash_cents=protocol['capital_twd']*100,budget_status='READY',positions=[])
    rules=LiveExecutionConfig.load(CODE_ROOT/'config/execution_live.json');p=policy()
    scan_rules=load_rules(CODE_ROOT/'config/scan_rules.json')
    market_history=defaultdict(list);seen=set();groups=[];events=[];counts=Counter()
    for folder in sorted((archive/'data/analysis/runs').glob('*')):
        source=folder/'multi_market_snapshot.json'
        if not source.exists():continue
        raw=read(source).get('quotes',[]);quotes=[]
        for q in raw:
            if q.get('symbol') not in protocol['universe'] or not q.get('quote_timestamp'):continue
            at=datetime.fromisoformat(q['quote_timestamp']).astimezone(TAIPEI);day=at.date().isoformat()
            if day not in protocol['development_dates']+protocol['validation_dates']:continue
            counts['archived_quote_observations']+=1
            if (q.get('quote_status')!='LIVE' or q.get('is_trial') or
                    at.time()<twse_oddlot_first_match(at.date())):
                counts['non_live_trial_or_pre_match']+=1;continue
            if any(type(q.get(k)) not in (int,float) or q[k]<=0 for k in
                   ('last_price','bid','ask','open','high','low')):
                counts['invalid_quote_fields']+=1;continue
            quotes.append(q)
        if not quotes:continue
        at=max(datetime.fromisoformat(q['quote_timestamp']) for q in quotes)
        scanned,ranked,_=scan_and_rank(quotes,scan_rules,at)
        rank={r['symbol']:r.get('quant_rank',r.get('rank')) for r in ranked}
        metrics={r['symbol']:r for r in scanned};new=[];full_pool=[]
        for q in quotes:
            symbol=q['symbol'];day=q['quote_timestamp'][:10]
            bars=[deepcopy(b) for d,b in sorted(history[symbol].items()) if d<day][-22:]
            c=dict(q,asset_type='ETF' if symbol=='0050' else 'STOCK',
                daily_bars=bars,market_history=deepcopy(market_history[symbol][-22:]),
                position=dict(ai_managed_qty=0),paper_position=dict(qty=0),
                pricing_policy='PLANNED_LIMIT',opportunity_buy_eligible=True,max_sell_qty=0,
                quant_rank=rank.get(symbol),metrics=metrics.get(symbol,{}).get('metrics',{}),
                components=metrics.get(symbol,{}).get('components',{}))
            e=assess(c,c['market_history']);c.update(strategy_evidence=e,eligible_strategies=e['eligible_strategies'])
            c['price_levels']=price_levels(c);apply_buy_limits([c],account,rules);refresh(c,account,rules,p)
            full_pool.append(deepcopy(c))
            if c['trade_setups'] and (day,symbol) not in seen:
                seen.add((day,symbol));event_id=day+'|'+symbol
                # The reference is chosen from information at t0, never from
                # later success: executable ask first, otherwise priority rank.
                reference=next((s for s in c['trade_setups'] if s['entry_basis']=='CURRENT_ASK'),c['trade_setups'][0])
                event=dict(event_id=event_id,symbol=symbol,as_of=q['quote_timestamp'],source_run=folder.name,
                    split='development' if day in protocol['development_dates'] else 'validation',
                    reference_setup_id=reference['setup_id'],candidate=c)
                events.append(event);new.append(event_id)
            elif not c['trade_setups']:counts.update(c['opportunity_status']['blockers'])
            market_history[symbol].append(dict(exchange_time=q['quote_timestamp'],price=q['last_price'],
                                               volume_shares=q.get('volume_shares',q.get('volume'))))
        if new:groups.append(dict(group_id=folder.name,as_of=at.isoformat(),events=new,selection_pool=full_pool))
    benchmark=dict(protocol=protocol,account=account,counts=dict(counts),events=events,groups=groups,
        historical_source_revisions=revisions,feature_scope='RECONSTRUCTED_PUBLIC_PAST_DAILY_AND_ARCHIVED_QUOTES',
        no_future_feature_check=all(all(b['date']<e['as_of'][:10] for b in e['candidate']['daily_bars']) for e in events))
    benchmark['benchmark_sha256']=digest(benchmark)
    save(output/'benchmark.json',benchmark)
    print(json.dumps(dict(events=len(events),groups=len(groups),splits=dict(Counter(e['split'] for e in events)),
                          benchmark_sha256=benchmark['benchmark_sha256'],no_future_features=benchmark['no_future_feature_check'])))


def label(archive, output):
    from operations.replay_archived_plans import load_daily
    archive=Path(archive);output=Path(output);b=read(output/'benchmark.json');quotes=defaultdict(list)
    for path in sorted((archive/'data/analysis/runs').glob('*/multi_market_snapshot.json')):
        for q in read(path).get('quotes',[]):quotes[q['symbol']].append(q)
    labels={};pool_routes={}
    for event in b['events']:
        c=event['candidate'];routes={}
        for setup in c['trade_setups']:
            routes[setup['setup_id']]=route_outcome(c,setup,quotes[c['symbol']],load_daily(output/'sources',c['symbol']))
        labels[event['event_id']]=dict(reference=routes[event['reference_setup_id']]['status'],routes=routes)
    for group in b['groups']:
        pool_routes[group['group_id']]={}
        for c in group.get('selection_pool',[]):
            pool_routes[group['group_id']][c['symbol']]={s['setup_id']:route_outcome(c,s,quotes[c['symbol']],
                load_daily(output/'sources',c['symbol'])) for s in c.get('trade_setups',[])}
    save(output/'labels.json',dict(benchmark_sha256=b['benchmark_sha256'],labels=labels,
        pool_routes=pool_routes,
        scope='QUOTED_ENTRY_AND_DAILY_BARRIER_PROXY_NOT_VERIFIED_COUNTERFACTUAL_FILLS'))
    print(json.dumps(dict(reference_outcomes=dict(Counter(x['reference'] for x in labels.values())),
                          reference_by_split={s:dict(Counter(labels[e['event_id']]['reference'] for e in b['events'] if e['split']==s))
                                              for s in ('development','validation')})))


def load_api_env(path):
    lines=Path(path).read_text(encoding='utf-8-sig').splitlines()
    for i,line in enumerate(lines):
        key,sep,value=line.strip().partition('=')
        if not sep or key not in ('OPENAI_API_KEY','OPENAI_BASE_URL','OPENAI_ORG_ID','OPENAI_PROJECT_ID'):continue
        value=value.strip().strip('"').strip("'")
        if key=='OPENAI_API_KEY' and not value and i+1<len(lines):value=lines[i+1].strip()
        if value:os.environ[key]=value


def archive_incomplete(directory,output):
    directory=Path(directory).resolve();root=Path(output).resolve()
    archive=root/'_attempts'/directory.name/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    if directory.parent!=root or not archive.resolve().is_relative_to(root):raise ValueError('UNSAFE_ATTEMPT_ARCHIVE')
    archive.parent.mkdir(parents=True,exist_ok=True)
    directory.rename(archive)
    return archive


def run(benchmark_path, output, api_env, shard=0, shards=1,reuse_from=None,retry_incomplete=False):
    # This mode deliberately never opens labels.json or outcome price files.
    permission_path=Path(__file__).resolve().parent.parent/'config/replay_api.json'
    if permission_path.exists() and read(permission_path).get('allow_model_requests') is not True:
        print('BLOCKED_USER_PAUSED_API: using existing evidence only',flush=True)
        return dict(blocked=True,reason='USER_PAUSED_API')
    quota_path=Path(benchmark_path).parent/'api_quota_stop.json'
    if quota_path.exists() and read(quota_path).get('blocked'):
        kind=read(quota_path).get('kind','API_QUOTA_EXHAUSTED')
        print('BLOCKED_'+kind+': model calls are stopped',flush=True)
        return dict(blocked=True,reason=kind)
    from config import CODE_ROOT
    from execution.auto_advice_executor import LiveExecutionConfig
    from opportunity_engine import select,policy
    from trading_agent import TradingTools,analyze,agent_model,execution_metadata,api_quota_exhausted
    from support_scout.trading import SupportCoordinator
    import strategy_catalog
    import shutil
    import time
    b=read(benchmark_path);output=Path(output);load_api_env(api_env)
    rules=LiveExecutionConfig.load(CODE_ROOT/'config/execution_live.json');p=policy()
    lookup={e['event_id']:e for e in b['events']};account=b['account']
    strategy=strategy_catalog.read_strategy(CODE_ROOT/'strategies/strategy_opportunity_v2.md')
    prior=read(Path(reuse_from).parent/'benchmark.json') if reuse_from else None
    prior_lookup={e['event_id']:e for e in prior['events']} if prior else {}
    source_manifest=Path(reuse_from).parent/(Path(reuse_from).name+'_source_manifest.json') if reuse_from else None
    manifest=read(source_manifest) if source_manifest and source_manifest.exists() else {}
    runtime_identical=bool(manifest) and all((CODE_ROOT/name).is_file() and
        hashlib.sha256((CODE_ROOT/name).read_bytes()).hexdigest()==sha for name,sha in manifest.items())
    for index,group in enumerate(b['groups']):
        if index%shards!=shard:continue
        if quota_path.exists() and read(quota_path).get('blocked'):
            print('BLOCKED_API_QUOTA: stopped before another model request',flush=True)
            return dict(blocked=True)
        dest=output/group['group_id'];result_path=dest/'result.json'
        if result_path.exists():
            previous_result=read(result_path)
            if previous_result['benchmark_sha256']!=b['benchmark_sha256']:raise ValueError('RESUME_BENCHMARK_MISMATCH')
            if retry_incomplete and previous_result['response']['validation']!='VALID':
                archive_incomplete(dest,output)
            else:
                if previous_result['response']['api']['implementation_sha256']!=execution_metadata(agent_model())['implementation_sha256']:
                    raise ValueError('RESUME_RUNTIME_MISMATCH')
                continue
        candidates=deepcopy(group['selection_pool']) if 'selection_pool' in group else [deepcopy(lookup[e]['candidate']) for e in group['events']]
        selected,selection=select(candidates,account,rules,p)
        # Reuse a real model observation only when all selected FULL inputs,
        # account, clock and runtime hash are identical. Added competitors that
        # change the shortlist require a new API run, regardless of outcomes.
        previous=Path(reuse_from)/group['group_id']/'result.json' if reuse_from else None
        if previous and previous.exists():
            old=read(previous)
            old_candidates=[deepcopy(prior_lookup[e]['candidate']) for e in group['events']]
            old_selected,_=select(old_candidates,prior['account'],rules,p)
            if (runtime_identical and selected==old_selected and account==prior['account'] and group['as_of']==old['group']['as_of']
                    and old['benchmark_sha256']==prior['benchmark_sha256']
                    and old['response']['api']['model']==agent_model()
                    and old['response']['api']['instructions_sha256']==execution_metadata(agent_model())['instructions_sha256']
                    and old['response']['api']['implementation_sha256']==execution_metadata(agent_model())['implementation_sha256']):
                shutil.copytree(previous.parent,dest,dirs_exist_ok=True)
                after={c['symbol']:c for c in old['all_candidates'] if c['symbol'] in old['selected_symbols']}
                old.update(group=group,benchmark_sha256=b['benchmark_sha256'],selection=selection,
                    all_candidates=[after.get(c['symbol'],c) for c in candidates],
                    exact_input_reuse=dict(source=str(previous),source_benchmark_sha256=prior['benchmark_sha256'],
                        selected_full_input_sha256=digest(selected),account_sha256=digest(account)))
                save(result_path,old)
                print(json.dumps(dict(group=group['group_id'],reuse='EXACT_FULL_SELECTED_INPUT_AND_RUNTIME')),flush=True)
                continue
        initial=dict(analysis_set=deepcopy(selected),paper_account=deepcopy(account),
                     evaluation=dict(as_of=group['as_of'],mode='FROZEN_OCTOBER_NO_EXECUTOR',external_search=False))
        at=datetime.fromisoformat(group['as_of']);frozen={c['symbol']:deepcopy(c) for c in selected}
        quote_keys=('symbol','last_price','bid','ask','open','high','low','volume','volume_shares',
                    'quote_status','quote_timestamp','is_trial','source','exchange_time')
        def quote(symbol,previous):return {k:deepcopy(frozen[symbol].get(k)) for k in quote_keys if k in frozen[symbol]}
        ctx=TradingTools(selected,rules,'paired-'+group['group_id'],dest/'agent_tools.json',quote,
            lambda:deepcopy(account),lambda:[],initial_input=initial,search_request=dict(tools=[]),
            quote_batch_reader=lambda symbols,previous:{s:quote(s,previous[s]) for s in symbols})
        cfg=read(CODE_ROOT/'config/support_agents.json')
        def observation(symbol,candidate,count,root):
            return dict(status='RAW_PRICE_OBSERVATION',technical=None,
                daily_bars=deepcopy(candidate['daily_bars']),history_status=candidate.get('daily_history_status',{}),
                limitations=['CORPORATE_ACTIONS_UNVERIFIED','ONLY_RECONSTRUCTED_PAST_DAILY_HISTORY'])
        ctx.support=SupportCoordinator(ctx,CODE_ROOT,cfg,observation)
        instructions=('這是歷史凍結資訊回放，唯一決策時點為 '+group['as_of']+'。'
            '工具均只回傳此時點資料，不按今天日期判過期，不使用未來結果；外部搜尋停用，沒有下單器。'
            '以正常策略獨立判斷，不為評估而強制交易。\n'+strategy['prompt'])
        wall_started=time.perf_counter()
        if selected:
            # Each worker is a separate process, so these historical packet
            # clocks cannot leak into other concurrent cases or live apps.
            with patch('support_scout.trading.now',return_value=at),patch('trading_agent.now',return_value=at):
                decision,response=analyze(ctx,instructions,model=agent_model())
        else:
            decision=dict(market_view='No surviving strategy plans',decisions=[])
            response=dict(validation='NOT_CALLED',api=execution_metadata(agent_model()))
        wall_seconds=time.perf_counter()-wall_started
        save(result_path,dict(benchmark_sha256=b['benchmark_sha256'],group=group,selection=selection,
            all_candidates=candidates,selected_symbols=[c['symbol'] for c in selected],
            decision=decision,response=response,wall_elapsed_seconds=wall_seconds,
            broker_access=False,executor_available=False,orders_sent=False))
        progress=dict(shard=shard,index=index,group=group['group_id'],selected=len(selected),validation=response['validation'])
        save(output/f'progress_{shard}.json',progress);print(json.dumps(progress),flush=True)
        if response.get('failure_kind')=='API_QUOTA_EXHAUSTED' or api_quota_exhausted(response.get('api',{}).get('error','')):
            save(quota_path,dict(blocked=True,kind='API_QUOTA_EXHAUSTED',group=group['group_id'],
                observed_at=datetime.now(timezone.utc).isoformat(),retry_automatically=False))
            return dict(blocked=True)


def score_variant(benchmark, labels, directory):
    from opportunity_engine import match_setup
    pool_labels=labels.get('pool_routes',{});labels=labels['labels'];results={};all_trades=[]
    for path in sorted(Path(directory).glob('*/result.json')):
        r=read(path)
        if benchmark.get('benchmark_sha256') and r.get('benchmark_sha256')!=benchmark['benchmark_sha256']:
            raise ValueError('REPLAY_BENCHMARK_MISMATCH:'+str(path))
        for identity in r['group']['events']:results[identity]=r
        for decision in r['decision'].get('decisions',[]):
            if decision.get('decision')!='BUY':continue
            candidate=next(c for c in r['all_candidates'] if c['symbol']==decision['symbol'])
            chosen=match_setup(candidate,decision)
            route=pool_labels.get(r['group']['group_id'],{}).get(decision['symbol'],{}).get(chosen['setup_id']) if chosen else None
            all_trades.append(dict(group_id=r['group']['group_id'],symbol=decision['symbol'],
                as_of=r['group'].get('as_of'),decision=decision,
                outcome=route['status'] if route and r['response']['validation']=='VALID' else 'UNCONFIRMED',
                in_first_episode_cohort=any(e['source_run']==r['group']['group_id'] and e['symbol']==decision['symbol']
                    for e in benchmark['events'])))
    details=[]
    for event in benchmark['events']:
        identity=event['event_id'];r=results.get(identity);truth=labels[identity]['reference']
        positive=(truth=='SUCCESS' or any(route['status']=='SUCCESS' for route in labels[identity]['routes'].values()))
        row=dict(event_id=identity,split=event['split'],reference=truth,opportunity_positive=positive,selected=False,buy=False,
                 chosen_outcome=None,miss=False,wrong_pick=False,caught=False,reason=None)
        if r is None:
            row.update(reason='REPLAY_NOT_COMPLETED',miss=positive)
            details.append(row);continue
        symbol=event['symbol'];row['selected']=symbol in r['selected_symbols']
        c=next((c for c in r['all_candidates'] if c['symbol']==symbol),None)
        d=next((d for d in r['decision']['decisions'] if d['symbol']==symbol),{})
        row['buy']=d.get('decision')=='BUY';row['model_reason']=d.get('reason')
        row['decision_warnings']=d.get('warnings',[])
        try:
            proposed=json.loads(r['response'].get('raw_response') or '{}')
            raw=next((d for d in proposed.get('decisions',[]) if d['symbol']==symbol),{})
        except (ValueError,TypeError):raw={}
        row['proposed_buy']=raw.get('decision')=='BUY'
        if not c or not c.get('trade_setups'):row['reason']='STRATEGY_FILTER'
        elif not row['selected']:row['reason']='SHORTLIST_OMISSION'
        elif r['response']['validation']!='VALID':
            from trading_agent import api_quota_exhausted
            row['reason']='API_QUOTA_EXHAUSTED' if api_quota_exhausted(r['response'].get('api',{}).get('error','')) else 'MODEL_ERROR'
        elif not row['buy']:
            warnings=set(d.get('warnings',[]))
            row['reason']=('PRETRADE_VETO' if 'PRETRADE_REVIEW_VETO' in warnings else
                'PRETRADE_UNAVAILABLE' if 'PRETRADE_REVIEW_UNAVAILABLE' in warnings else
                'SUPPORT_VETO' if any(w.startswith('SUPPORT_') for w in warnings) else
                'DETERMINISTIC_VALIDATION_VETO' if row['proposed_buy'] else 'AGENT_WAIT')
        else:
            chosen=match_setup(c,d)
            route=labels[identity]['routes'].get(chosen['setup_id']) if chosen else None
            row['chosen_outcome']=route['status'] if route else 'OUTSIDE_FROZEN_ROUTE_SET'
            row['wrong_pick']=row['chosen_outcome']=='FAILURE'
            row['caught']=row['chosen_outcome']=='SUCCESS'
            row['reason']='WRONG_PLAN_PICK' if row['wrong_pick'] else (
                'ENTRY_OR_OUTCOME_UNCONFIRMED' if not row['caught'] else 'SUCCESSFUL_PICK')
        row['miss']=positive and not row['caught']
        details.append(row)
    for event,row in zip(benchmark['events'],details):
        later=[]
        for trade in all_trades:
            if (trade['symbol']!=event['symbol'] or trade['outcome']!='SUCCESS' or not trade.get('as_of')
                    or not event.get('as_of') or not event.get('candidate')):continue
            # A later reevaluation only captures the ORIGINAL ROD opportunity
            # if it still buys an exact original route on that same day.
            if trade['as_of']<event['as_of'] or trade['as_of'][:10]!=event['as_of'][:10]:continue
            original=match_setup(event['candidate'],trade['decision'])
            if original and labels[event['event_id']]['routes'][original['setup_id']]['status']=='SUCCESS':
                later.append(dict(group_id=trade['group_id'],as_of=trade['as_of'],setup_id=original['setup_id']))
        row['original_plan_capture_evidence']=later
        row['late_capture']=bool(later) and not row['caught']
        row['same_day_miss']=row['opportunity_positive'] and not (row['caught'] or later)
    def aggregate(rows):
        positives=sum(r['opportunity_positive'] for r in rows)
        caught=sum(r['opportunity_positive'] and r['caught'] for r in rows)
        wins=sum(r['caught'] for r in rows);fp=sum(r['wrong_pick'] for r in rows);fn=sum(r['miss'] for r in rows)
        return dict(events=len(rows),reference_positive=sum(r['reference']=='SUCCESS' for r in rows),
            opportunity_positive=positives,successful_picks=wins,wrong_picks=fp,misses=fn,
            pick_precision=wins/(wins+fp) if wins+fp else None,capture_recall=caught/positives if positives else None,
            miss_rate=fn/positives if positives else None,error_count=fp+fn,
            first_response_misses=fn,same_day_original_plan_misses=sum(r['same_day_miss'] for r in rows),
            late_captures=sum(r['late_capture'] for r in rows),
            same_day_original_plan_recall=sum(r['opportunity_positive'] and not r['same_day_miss'] for r in rows)/positives if positives else None,
            unique_error_events=sum(r['wrong_pick'] or r['miss'] for r in rows),
            process_errors=sum(r['reason'] in ('MODEL_ERROR','API_QUOTA_EXHAUSTED','PRETRADE_UNAVAILABLE','REPLAY_NOT_COMPLETED') for r in rows),
            unique_errors_including_process=sum(r['wrong_pick'] or r['miss'] or r['reason'] in
                ('MODEL_ERROR','API_QUOTA_EXHAUSTED','PRETRADE_UNAVAILABLE','REPLAY_NOT_COMPLETED') for r in rows),
            shortlist_recall=sum(r['opportunity_positive'] and r['selected'] for r in rows)/positives if positives else None,
            unscored_reference=sum(r['reference'] not in ('SUCCESS','FAILURE') for r in rows),
            unscored_buys=sum(r['buy'] and r['chosen_outcome'] not in ('SUCCESS','FAILURE') for r in rows),
            incomplete_replays=sum(r['reason']=='REPLAY_NOT_COMPLETED' for r in rows),
            decision_causes=dict(Counter(r['reason'] for r in rows)),
            miss_causes=dict(Counter(r['reason'] for r in rows if r['miss'])))
    trade_counts=Counter(r['outcome'] for r in all_trades)
    resolved=trade_counts['SUCCESS']+trade_counts['FAILURE']
    return dict(overall=aggregate(details),development=aggregate([r for r in details if r['split']=='development']),
        validation=aggregate([r for r in details if r['split']=='validation']),events=details,
        all_snapshot_buys=dict(count=len(all_trades),successful=trade_counts['SUCCESS'],wrong=trade_counts['FAILURE'],
            unscored=len(all_trades)-resolved,precision=trade_counts['SUCCESS']/resolved if resolved else None,
            outside_first_episode_cohort=sum(not r['in_first_episode_cohort'] for r in all_trades),
            scope='ALL_FINAL_BUYS_IN_INDEPENDENT_SNAPSHOTS_REPEATED_SYMBOLS_ARE_NOT_PORTFOLIO_TRADES',decisions=all_trades))


def compare(output):
    output=Path(output);b=read(output/'benchmark.json');l=read(output/'labels.json')
    if b['benchmark_sha256']!=l['benchmark_sha256']:raise ValueError('BENCHMARK_LABEL_MISMATCH')
    result=dict(benchmark_sha256=b['benchmark_sha256'],protocol=b['protocol'],
        baseline=score_variant(b,l,output/'baseline'),revised=score_variant(b,l,output/'revised'),
        metric_definitions=dict(pick_precision='Winning chosen routes / resolved chosen routes',
            capture_recall='Episodes with any successful pre-frozen executable route caught by a winning chosen route / all such positive episodes',
            same_day_capture='Same-day later BUY must match an exact original route; first-response deferral remains a separate error',
            all_snapshot_precision='All final successful BUY decisions / all resolved BUY decisions including competing symbols; independent snapshots may repeat stocks',
            errors='Wrong chosen routes + missed positive reference episodes; overlapping event IDs also reported',
            uncertainty='Unconfirmed fills and ambiguous barriers stay visible; no credit as successful capture'))
    save(output/'comparison.json',result)
    print(json.dumps({v:{s:result[v][s] for s in ('overall','development','validation')} for v in ('baseline','revised')},indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('mode',choices=('prepare','label','run','compare'))
    p.add_argument('--archive-root',type=Path);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--benchmark',type=Path);p.add_argument('--api-env',type=Path)
    p.add_argument('--reuse-from',type=Path)
    p.add_argument('--retry-incomplete',action='store_true')
    p.add_argument('--shard',type=int,default=0);p.add_argument('--shards',type=int,default=1)
    a=p.parse_args()
    if a.mode=='prepare':prepare(a.archive_root,a.output)
    elif a.mode=='label':label(a.archive_root,a.output)
    elif a.mode=='run':
        status=run(a.benchmark,a.output,a.api_env,a.shard,a.shards,a.reuse_from,a.retry_incomplete)
        if status and status.get('blocked'):raise SystemExit(75)
    else:compare(a.output)


if __name__=='__main__':main()
