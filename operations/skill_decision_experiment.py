"""Two-factor replay: decision evidence rules x loaded research Skill."""
import argparse
from collections import Counter
from copy import deepcopy
from pathlib import Path

from operations.expanded_opportunity_replay import read,save,digest,score_variant

VARIANTS=('baseline_no_skill','baseline_skill','revised_no_skill','revised_skill')


def export_runtimes(code_root,runtime_root,output):
    import hashlib
    import shutil
    import subprocess
    from datetime import datetime,timezone
    code_root=Path(code_root).resolve();runtime_root=Path(runtime_root).resolve();output=Path(output)
    paths=subprocess.check_output(['git','-C',str(code_root),'ls-files','--cached','--others','--exclude-standard','-z']).decode().split('\0')
    manifests={};stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    for variant in VARIANTS:
        dest=runtime_root/('stockllm-ab-'+variant)
        if not dest.resolve().is_relative_to(runtime_root):raise ValueError('UNSAFE_RUNTIME_PATH')
        if (dest/'.env').exists() or (dest/'.trading-state.json').exists():raise ValueError('RUNTIME_HAS_ACCOUNT_BINDING')
        copied=[]
        for name in paths:
            p=Path(name)
            if (not name or p.name.startswith('.env') or p.name=='.trading-state.json'
                    or any(part in ('CapitalAPI','reports','data','.git','__pycache__') for part in p.parts)
                    or p.suffix.lower() not in ('.py','.md','.json','.toml','.txt','.ps1','.cmd')):continue
            source=code_root/p
            if not source.is_file():continue
            target=dest/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target);copied.append(p)
        for name,key,value in [('entry_decision.json','enabled',variant.startswith('revised')),
                ('research_skills.json','entry_research_enabled',not variant.endswith('no_skill'))]:
            path=dest/'config'/name;data=read(path);data[key]=value;save(path,data)
        manifest={str(p).replace('\\','/'):hashlib.sha256((dest/p).read_bytes()).hexdigest() for p in copied}
        manifests[variant]=manifest;path=output/(variant+'_source_manifest.json')
        if path.exists():save(output/'source_attempts'/stamp/path.name,read(path))
        save(path,manifest)
    excluded={'config/entry_decision.json','config/research_skills.json'}
    common={k:v for k,v in manifests[VARIANTS[0]].items() if k not in excluded}
    if any({k:v for k,v in m.items() if k not in excluded}!=common for m in manifests.values()):
        raise ValueError('FACTORIAL_SOURCE_MISMATCH')
    save(output/'source_controls.json',dict(common_source_sha256=digest(common),
        controlled_files=len(common),only_factor_configs_differ=True,
        source_commit=subprocess.check_output(['git','-C',str(code_root),'rev-parse','HEAD']).decode().strip()))
    print(dict(runtime_root=str(runtime_root),variants=list(VARIANTS),no_credentials_exported=True))


def freeze(source,archive,output):
    source=Path(source);archive=Path(archive);output=Path(output)
    old=read(source/'benchmark.json');labels=read(source/'labels.json')
    if old['benchmark_sha256']!=labels['benchmark_sha256']:raise ValueError('SOURCE_LABEL_MISMATCH')
    quotes={}
    for path in sorted((archive/'data/analysis/runs').glob('*/multi_market_snapshot.json')):
        for q in read(path).get('quotes',[]):
            if q.get('quote_status')=='LIVE' and q.get('is_trial') is False:
                quotes[q['symbol'],q.get('quote_timestamp')]=q
    b=deepcopy(old);b.pop('benchmark_sha256');verified=0;unknown=0
    candidates=[e['candidate'] for e in b['events']]+[c for g in b['groups'] for c in g['selection_pool']]
    for c in candidates:
        for h in c.get('market_history',[]):
            if h['exchange_time']>c['quote_timestamp']:raise ValueError('FUTURE_INTRADAY_INPUT')
            q=quotes.get((c['symbol'],h['exchange_time']))
            valid=bool(q and q.get('last_price')==h.get('price') and q.get('retrieval_mode')!='CACHE'
                and (q.get('received_at') or q['quote_timestamp'])<=c['quote_timestamp'])
            h.update(symbol=c['symbol'],quote_status='LIVE' if valid else 'UNKNOWN',
                is_trial=False if valid else None,retrieval_mode=q.get('retrieval_mode') if valid else None,
                source_snapshot_sha256=digest(q) if valid else None)
            verified+=valid;unknown+=not valid
    b['protocol']=dict(b['protocol'],version='skill-decision-factorial-v1',
        data_role='ALL_OCTOBER_IS_DEVELOPMENT_NOT_UNSEEN_VALIDATION',
        factors=dict(decision_evidence=['disabled','enabled'],entry_skill=['disabled','enabled']),
        hypotheses=['Recovery requires last completed green higher close and current MA5 reclaim',
            'First live breakout with modeled target requires a distinct prior verified LIVE continuation quote'],
        no_fitted_numeric_thresholds=True,market_outcomes_hidden_from_model=True,
        success_criterion='Unchanged original entry target invalidation horizon; FP and FN both errors',
        no_required_trades_or_research_quota=True)
    b['benchmark_sha256']=digest(b);labels['benchmark_sha256']=b['benchmark_sha256']
    save(output/'benchmark.json',b);save(output/'labels.json',labels)
    save(output/'protocol.json',dict(b['protocol'],source_benchmark_sha256=old['benchmark_sha256'],
        benchmark_sha256=b['benchmark_sha256'],verified_history_rows=verified,unknown_history_rows=unknown,
        variants=list(VARIANTS),frozen_reference_plans_unchanged=True))
    print(dict(episodes=len(b['events']),groups=len(b['groups']),verified_history_rows=verified,unknown_history_rows=unknown))


def usage(directory):
    totals=Counter();elapsed=0;called=0;failed=0;skill_modes=Counter()
    infrastructure=0;decision_errors=0;usage_complete=True;provider_rows={}
    for path in Path(directory).glob('*/result.json'):
        r=read(path);response=r['response'];called+=response['validation']!='NOT_CALLED'
        failed+=response['validation']=='ERROR';elapsed+=r.get('wall_elapsed_seconds',0)
        if response['validation']!='NOT_CALLED':
            usage_complete &= bool(response.get('api',{}).get('usage')) and response.get('api',{}).get('usage_accounting_complete',True)
        if response['validation']=='ERROR':
            error=response.get('api',{}).get('error','')
            if error.startswith(('ValueError:','TypeError:')):decision_errors+=1
            else:infrastructure+=1
        policy=(response.get('api') or {}).get('research_skill_policy')
        if policy:skill_modes[str(policy['enabled'])]+=1
        for data in (response.get('api',{}).get('usage',{}),
                     response.get('pretrade_review',{}).get('api',{}).get('usage',{})):
            for key in ('input_tokens','output_tokens','cached_input_tokens','cache_write_tokens','reasoning_tokens','requests'):
                totals[key]+=data.get(key,0)
        totals['tool_calls']+=len(response.get('tool_calls',[]))
        totals['support_calls']+=len(response.get('support_research',{}).get('calls',[]))
        for filename in ('api_usage_requests.json','pretrade_usage_requests.json'):
            if (path.parent/filename).exists():
                for row in read(path.parent/filename):
                    if row.get('response_id'):provider_rows[row['response_id']]=row
    from operations.audit_api_usage import estimate_standard_sol
    return dict(totals,model_groups=called,model_errors=failed,wall_seconds=elapsed,skill_modes=dict(skill_modes),
        infrastructure_errors=infrastructure,decision_errors=decision_errors,
        usage_complete=usage_complete,interpret_as_model_efficiency=infrastructure==0,
        estimated_standard_usd=estimate_standard_sol(list(provider_rows.values())) if provider_rows or not called else None)


def compare(output):
    output=Path(output);b=read(output/'benchmark.json');labels=read(output/'labels.json')
    if b['benchmark_sha256']!=labels['benchmark_sha256']:raise ValueError('LABEL_MISMATCH')
    variants={v:dict(score=score_variant(b,labels,output/v),efficiency=usage(output/v)) for v in VARIANTS}
    valid=all(x['efficiency']['infrastructure_errors']==0 and x['score']['overall']['incomplete_replays']==0 for x in variants.values())
    for x in variants.values():
        x['decision_quality']=dict(status='EVALUATED' if valid else 'UNAVAILABLE_INFRASTRUCTURE_FAILURE',
            hit_rate=x['score']['all_snapshot_buys']['precision'] if valid else None,
            same_day_miss_rate=1-x['score']['overall']['same_day_original_plan_recall'] if valid and x['score']['overall']['same_day_original_plan_recall'] is not None else None,
            operational_misses=x['score']['overall']['same_day_original_plan_misses'],
            raw_score_is_operational_only=not valid)
    controls=[]
    for prefix in ('baseline','revised'):
        for g in b['groups']:
            a=output/(prefix+'_no_skill')/g['group_id'];z=output/(prefix+'_skill')/g['group_id']
            if not (a/'result.json').exists() or not (z/'result.json').exists():continue
            ar=read(a/'result.json');zr=read(z/'result.json')
            if ar['selected_symbols']!=zr['selected_symbols']:raise ValueError('SKILL_CHANGED_SHORTLIST')
            both=(a/'agent_model_input.json').exists() and (z/'agent_model_input.json').exists()
            same=(read(a/'agent_model_input.json')==read(z/'agent_model_input.json')) if both else not ar['selected_symbols']
            if not same:raise ValueError('SKILL_COMPARISON_INPUT_MISMATCH:'+g['group_id'])
            controls.append(dict(variant=prefix,group_id=g['group_id'],identical_model_input=True))
    result=dict(protocol=read(output/'protocol.json'),variants=variants,input_controls=controls,
        records_complete=all(v['score']['overall']['incomplete_replays']==0 for v in variants.values()),
        complete=valid,decision_comparison_valid=valid,
        limits=['October outcomes were already inspected: diagnostic development only.',
            'One stochastic replay per factor cell cannot establish a reliable causal Skill improvement.',
            'Regular-market barrier proxies and quoted entries are not verified odd-lot fills.',
            'Independent empty-book snapshots are not a settled portfolio backtest.',
            'No whole-market opportunity benchmark or live scout selection is tested.'])
    save(output/'comparison.json',result)
    if not valid:
        print(dict(status='UNAVAILABLE_INFRASTRUCTURE_FAILURE',hit_rate=None,miss_rate=None,
            model_errors={v:x['efficiency']['model_errors'] for v,x in variants.items()}))
        return
    print({v:dict(first_errors=x['score']['overall']['error_count'],
        first_misses=x['score']['overall']['first_response_misses'],
        same_day_misses=x['score']['overall']['same_day_original_plan_misses'],
        buys=x['score']['all_snapshot_buys']['count'],wrong=x['score']['all_snapshot_buys']['wrong'],
        wins=x['score']['all_snapshot_buys']['successful'],precision=x['score']['all_snapshot_buys']['precision']) for v,x in variants.items()})


def resume_api_quota(output):
    output=Path(output);quota=output/'api_quota_stop.json'
    if quota.exists():
        previous=read(quota)
        if previous.get('kind')=='USER_PAUSED_API':
            raise ValueError('USER_API_PAUSE_CANNOT_BE_RELEASED_BY_CREDIT_RESTORE')
        save(output/'api_quota_previous.json',previous)
        save(quota,dict(blocked=False,previous=previous,reason='EXPLICIT_OPERATOR_RESUME_AFTER_CREDIT_RESTORE'))
    print('Quota stop released; retry incomplete runs explicitly. This does not verify credits.')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=('freeze','compare','resume-api-quota','export'))
    parser.add_argument('--source',type=Path);parser.add_argument('--archive-root',type=Path)
    parser.add_argument('--code-root',type=Path,default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--runtime-root',type=Path)
    parser.add_argument('--output',type=Path,required=True);a=parser.parse_args()
    if a.mode=='freeze':freeze(a.source,a.archive_root,a.output)
    elif a.mode=='compare':compare(a.output)
    elif a.mode=='export':export_runtimes(a.code_root,a.runtime_root,a.output)
    else:resume_api_quota(a.output)


if __name__=='__main__':main()
