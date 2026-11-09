"""Read-only archive audit and independently labelled opportunity scoring.

No broker/model calls, no revised strategy replay, no guessed market outcomes.
Every labelled missed opportunity is an FN, including capacity/guard misses.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

STAGES = ('pool', 'shortlisted', 'buy', 'submitted', 'filled')
OUTCOMES = {'SUCCESS', 'FAILURE', 'PENDING', 'AMBIGUOUS', 'UNAVAILABLE'}


def read_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else default


def stamp(value):
    at=datetime.fromisoformat(value)
    if at.tzinfo is None:raise ValueError('TIMEZONE_REQUIRED')
    return at


def fraction(top, bottom):
    return top/bottom if bottom else None


def score_events(events, as_of=None):
    """Score all benchmark observations; a valid block never removes an FN."""
    as_of=as_of or datetime.now(timezone.utc)
    seen=set();scored=[];unscored=Counter();quality_errors=[]
    for event in events:
        identity=event['event_id']
        if identity in seen:raise ValueError('DUPLICATE_OPPORTUNITY_EVENT: '+identity)
        seen.add(identity)
        for stage in STAGES:
            if type(event['stages'].get(stage)) is not bool:
                raise ValueError('EXPLICIT_STAGE_BOOLEAN_REQUIRED: '+stage)
        if event.get('quality_error'):quality_errors.append(identity)
        status=(event.get('outcome') or {}).get('status','UNAVAILABLE')
        if status not in OUTCOMES:raise ValueError('INVALID_OUTCOME')
        if status not in ('SUCCESS','FAILURE'):
            unscored[status]+=1
            continue
        label=event['outcome']
        refs=label.get('evidence_refs')
        if (not isinstance(refs,list) or not refs or any(not isinstance(ref,str) or not ref for ref in refs)
                or not label.get('criterion_id')
                or stamp(label['plan_available_at'])>stamp(event['decision_at'])
                or stamp(label['available_at'])<=stamp(event['decision_at'])):
            raise ValueError('INDEPENDENT_CHRONOLOGICAL_OUTCOME_EVIDENCE_REQUIRED')
        if stamp(label['available_at'])>as_of:
            unscored['PENDING']+=1
            continue
        scored.append(event)
    criteria={event['outcome']['criterion_id'] for event in scored}
    if len(criteria)>1:raise ValueError('MIXED_SUCCESS_CRITERIA')
    stages={}
    for stage in STAGES:
        counts=Counter();misses=[]
        for event in scored:
            positive=event['outcome']['status']=='SUCCESS';picked=event['stages'][stage]
            key=('TP' if picked else 'FN') if positive else ('FP' if picked else 'TN')
            counts[key]+=1
            if key=='FN':misses.append(event['event_id'])
        tp,fp,fn,tn=(counts[k] for k in ('TP','FP','FN','TN'))
        stages[stage]=dict(TP=tp,FP=fp,FN=fn,TN=tn,precision=fraction(tp,tp+fp),
            recall=fraction(tp,tp+fn),miss_rate=fraction(fn,tp+fn),
            f1=fraction(2*tp,2*tp+fp+fn),error_count=fp+fn,missed_event_ids=misses)
    return dict(observations=len(events),labelled=len(scored),unscored=dict(unscored),
        label_coverage=fraction(len(scored),len(events)),criterion_id=next(iter(criteria),None),
        stages=stages,quality_error_count=len(quality_errors),quality_error_event_ids=quality_errors,
        scope='LABELLED_BENCHMARK_OBSERVATIONS_ONLY',whole_market_recall=None,
        warnings=['Unlabelled, immature and ambiguous outcomes remain visible and are not counted as correct.',
                  'Capacity limits and valid guard reasons still count as misses when the benchmark outcome is SUCCESS.',
                  'Repeated scans are observations; independent opportunity episodes must be deduplicated before a performance claim.'])


def cycle_events(folder):
    manifest=read_json(folder/'run.json',{})
    if not manifest.get('run_id'):return []
    pool=read_json(folder/'opportunity_pool_after_selection.json')
    full_pool=pool is not None
    selected=read_json(folder/'analysis_set.json',[])
    pool=pool if pool is not None else selected
    selected_map={c['symbol']:c for c in selected}
    decisions={d['symbol']:d for d in read_json(folder/'selection_decision.json',{}).get('decisions',[])}
    execution=read_json(folder/'execution_report.json',{})
    executions={row['symbol']:row for row in execution.get('results',[]) if row.get('symbol')}
    agent=read_json(folder/'agent_response.json') or read_json(folder/'openai_response.json',{})
    support=agent.get('support_research') or read_json(folder/'support_research.json',{})
    pretrade=agent.get('pretrade_review') or {}
    events=[]
    for candidate in pool:
        symbol=candidate['symbol'];decision=decisions.get(symbol,{})
        plans=candidate.get('trade_setups',[]);current=selected_map.get(symbol,{})
        sent=executions.get(symbol,{})
        qualified=bool(plans);reason=None
        if manifest.get('status')=='BLOCKED_BUDGET':reason='BUDGET_PAUSE'
        elif manifest.get('status')=='ERROR' or manifest.get('ai_validation')=='ERROR':reason='PIPELINE_OR_MODEL_FAILURE'
        elif qualified and symbol not in selected_map:reason='SHORTLIST_OMISSION'
        elif qualified and not current.get('trade_setups') and (current.get('opportunity_status') or {}).get('blockers'):
            reason='PLAN_INVALIDATED_AFTER_REFRESH'
        elif any(row.get('symbol')==symbol and (row.get('review') or {}).get('verdict')=='REJECT'
                 for row in support.get('calls',[])):reason='INDEPENDENT_RESEARCH_VETO'
        elif any(row.get('symbol')==symbol and row.get('approved') is False for row in pretrade.get('reviews',[])):
            reason='PRETRADE_REVIEW_VETO'
        elif qualified and decision.get('decision')!='BUY':reason='UNVERIFIED_QUALIFIED_NON_BUY_REASON'
        elif decision.get('decision')=='BUY' and sent.get('broker_order_sent') is not True:
            reason='EXECUTION_NOT_CONFIRMED'
        decision_at=(manifest.get('llm_request_started_at') or manifest.get('request_completed_at')
                     or manifest.get('started_at'))
        events.append(dict(event_id=manifest['run_id']+'|'+symbol,run_id=manifest['run_id'],symbol=symbol,
            decision_at=decision_at,agent_version=(manifest.get('runtime') or {}).get('agent_version'),
            trading_mode=manifest.get('trading_mode'),full_pool_recorded=full_pool,qualified_plan=qualified,
            plan_sha256=hashlib.sha256(json.dumps(plans,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            plan_count=len(plans),decision=decision.get('decision'),reason=reason,model_reason=decision.get('reason'),
            quality_error=reason in ('UNVERIFIED_QUALIFIED_NON_BUY_REASON','PIPELINE_OR_MODEL_FAILURE'),
            stages=dict(pool=True,shortlisted=symbol in selected_map,buy=decision.get('decision')=='BUY',
                        submitted=decision.get('decision')=='BUY' and sent.get('broker_order_sent') is True,
                        filled=decision.get('decision')=='BUY' and int(sent.get('filled_quantity') or 0)>0),
            outcome=dict(status='UNAVAILABLE')))
    return events


def apply_labels(events, registry):
    """Include independent benchmark opportunities omitted from the archive too."""
    by_id={event['event_id']:event for event in events}
    if len(by_id)!=len(events):raise ValueError('DUPLICATE_ARCHIVE_EVENT')
    seen=set()
    for label in registry.get('labels',[]):
        identity=label['event_id']
        if identity in seen:raise ValueError('DUPLICATE_LABEL')
        seen.add(identity)
        event=by_id.get(identity)
        if event is None:
            # This is how an externally established opportunity outside the
            # scout/seed pool enters the FN denominator instead of disappearing.
            event=dict(event_id=identity,symbol=label['symbol'],decision_at=label['decision_at'],
                stages={stage:False for stage in STAGES},reason='OUTSIDE_OBSERVED_POOL',
                quality_error=False,external_benchmark=True)
            events.append(event);by_id[identity]=event
        if label.get('plan_sha256') and label['plan_sha256']!=event.get('plan_sha256'):
            raise ValueError('LABEL_PLAN_MISMATCH')
        event['outcome']={key:label.get(key) for key in
            ('status','evidence_refs','plan_available_at','available_at')}
        event['outcome']['criterion_id']=registry['criterion_id']
    return events


def audit(root, registry=None, agent_version=None):
    root=Path(root);folders=sorted((root/'data/analysis/runs').glob('*'))
    events=[];run_count=0;with_pool=0;missing_manifest=0;versions=Counter();first=None;last=None
    for folder in folders:
        manifest=read_json(folder/'run.json',{})
        if not manifest.get('run_id'):
            missing_manifest+=1
            continue
        version=(manifest.get('runtime') or {}).get('agent_version')
        if agent_version and version!=agent_version:continue
        run_count+=1;versions[str(version)]+=1
        at=manifest.get('started_at')
        if at:first=min(first or at,at);last=max(last or at,at)
        with_pool+=(folder/'opportunity_pool_after_selection.json').is_file()
        events.extend(cycle_events(folder))
    if registry:events=apply_labels(events,registry)
    evaluation=score_events(events)
    return dict(schema_version='opportunity-evaluation-v1',state_root=str(root.resolve()),
        generated_at=datetime.now(timezone.utc).isoformat(),agent_version_filter=agent_version,
        archive=dict(runs=run_count,runs_with_full_opportunity_pool=with_pool,missing_manifests=missing_manifest,
                     first=first,last=last,
                     agent_versions=dict(versions)),evaluation=evaluation,
        blockers=dict(Counter(event.get('reason') for event in events if event.get('reason'))),
        observations=events,
        limitations=['No new model decisions, outcome prices or fills were generated.',
                     'Archive-only recall cannot measure opportunities absent from the saved universe.',
                     'SUCCESS labels require a separately frozen criterion, plan timing and later market evidence.',
                     'Daily OHLC with an unresolved target/stop ordering must be labelled AMBIGUOUS.'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root',type=Path,required=True)
    parser.add_argument('--labels',type=Path)
    parser.add_argument('--agent-version')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--summary-only',action='store_true')
    args=parser.parse_args()
    if not (args.state_root/'data/analysis/runs').is_dir():parser.error('Analysis run archive does not exist')
    if args.labels and not args.labels.is_file():parser.error('Outcome label registry does not exist')
    result=audit(args.state_root,read_json(args.labels) if args.labels else None,args.agent_version)
    if args.summary_only:result.pop('observations')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    evaluation=result['evaluation']
    print(json.dumps(dict(archive=result['archive'],observations=evaluation['observations'],
        labelled=evaluation['labelled'],unscored=evaluation['unscored'],
        quality_error_count=evaluation['quality_error_count'],
        buy_precision=evaluation['stages']['buy']['precision'],
        opportunity_capture_recall=evaluation['stages']['filled']['recall'],
        miss_rate=evaluation['stages']['filled']['miss_rate']),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
