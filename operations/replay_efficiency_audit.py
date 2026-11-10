"""Explain saved replay cost and repetition without making any provider call."""
import argparse
from collections import Counter,defaultdict
from pathlib import Path
from statistics import median
import json

from operations.audit_api_usage import estimate_standard_sol


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def phase(source):
    if '/replications/' in source:return 4,'stability_repeats'
    if 'skill_decision_ab_20261109/' in source:return 3,'factorial_matrix'
    if 'october_expanded_20261109/competitive/' in source:return 2,'competitive_pool_correction'
    if 'october_expanded_20261109/' in source:return 1,'initial_expanded_replay'
    if 'october_replay_20261109/current_agent/' in source:return 0,'single_case_pilot'
    return 9,'other'


def summarize(rows):
    return dict(requests=len(rows),input_tokens=sum(r['input_tokens'] for r in rows),
        output_tokens=sum(r['output_tokens'] for r in rows),
        reasoning_tokens_included_in_output=sum(r.get('reasoning_tokens',0) for r in rows),
        estimated_usd_at_saved_standard_rates=estimate_standard_sol(rows))


def audit(reports,ledger):
    reports=Path(reports);ledger=read(ledger);rows=ledger['responses'];owners={};phases=defaultdict(list)
    for row in rows:
        source=min(row['sources'],key=phase);owners[row['response_id']]=source
        phases[phase(source)[1]].append(row)
    folders=sorted({str(Path(s).parent).replace('\\','/') for s in owners.values()})
    matched={};ambiguous=0;tool_counts=Counter();sdk_counts=Counter();duplicates=Counter();verdicts=Counter()
    transport=[];paired_runs=0;all_support_calls=0;timings=defaultdict(list)
    for name in folders:
        folder=reports/name
        if (folder/'result.json').exists():
            elapsed=read(folder/'result.json').get('wall_elapsed_seconds')
            if elapsed is not None:timings[phase(name+'/api_usage_requests.json')[1]].append(elapsed)
        if (folder/'agent_tools.json').exists():
            calls=read(folder/'agent_tools.json');tool_counts.update(c['tool'] for c in calls)
            identities=Counter((c['tool'],json.dumps(c.get('arguments'),sort_keys=True)) for c in calls)
            for (tool,args),count in identities.items():duplicates[tool]+=max(0,count-1)
        if (folder/'agent_sdk_tools.json').exists():
            sdk_counts.update(c['name'] for c in read(folder/'agent_sdk_tools.json') if c.get('type')=='function_call')
        if not (folder/'support_research.json').exists():continue
        support=read(folder/'support_research.json');records=read(folder/'api_usage_requests.json')
        calls=support.get('calls',[]);all_support_calls+=len(calls);paired_runs+=bool(calls)
        for call in calls:
            verdicts[(call.get('review') or {}).get('verdict',call['status'])]+=1
            usage=call.get('usage') or {}
            if not usage:continue
            candidates=[r for r in records if r.get('response_id') not in matched and
                r['input_tokens']==usage.get('input_tokens') and r['output_tokens']==usage.get('output_tokens')]
            if len(candidates)!=1:ambiguous+=1;continue
            identity=candidates[0]['response_id']
            if owners.get(identity)!=str(Path(name)/'api_usage_requests.json').replace('\\','/'):continue
            matched[identity]='support_'+call['role']
        for packet in support.get('packets',[]):
            core=packet['core'];duplicated=0
            if core.get('entry_context'):
                duplicated+=sum(len(json.dumps(e['values'],ensure_ascii=False)) for e in core.get('evidence',[])
                    if e.get('kind')=='ENTRY_CONTEXT' and e.get('values')==core['entry_context'])
            if packet.get('tasks')==core.get('agent_tasks'):duplicated+=len(json.dumps(packet['tasks'],ensure_ascii=False))
            from support_scout.transport import model_packet,expand_packet
            wire=model_packet(packet)
            if expand_packet(wire)!=packet:raise ValueError('SAVED_PACKET_ROUNDTRIP_FAILED')
            transport.append(dict(source=name,packet_id=packet['packet_id'],
                old_characters=len(json.dumps(packet,ensure_ascii=False)),duplicated_characters=duplicated,
                new_characters=len(json.dumps(wire,ensure_ascii=False,separators=(',',':')))))
    actors=defaultdict(list)
    for row in rows:
        actor=matched.get(row['response_id']) or ('pretrade_review' if row.get('role')=='pretrade_review' else 'main_or_unmatched_sdk')
        actors[actor].append(row)
    initial_unreused=[r for r in phases['initial_expanded_replay'] if not any('/competitive/' in s for s in r['sources'])]
    before=sum(p['old_characters'] for p in transport);after=sum(p['new_characters'] for p in transport)
    return dict(scope='Saved 2026-11-09 task replies only; no new inference or orders',
        totals=summarize(rows),phases={k:summarize(v) for k,v in phases.items()},
        actors={k:summarize(v) for k,v in actors.items()},
        actor_attribution=dict(method='Unique input/output token match against saved specialist call usage; remainder stays unclassified main/SDK',
            matched_specialist_requests=len(matched),ambiguous_matches=ambiguous),
        unique_paid_analysis_contexts=len(folders),specialist_verdicts=dict(verdicts),
        main_tool_calls=dict(tool_counts),parent_sdk_tool_calls=dict(sdk_counts),
        repeated_same_tool_arguments_within_context=dict(duplicates),
        initial_replay_requests_not_reused_after_pool_correction=summarize(initial_unreused),
        grouping_opportunity=dict(specialist_calls=all_support_calls,contexts_with_specialist_calls=paired_runs,
            note='Batching can reduce parent round trips; specialist calls themselves remain independent and are not removed.'),
        recorded_group_durations={k:dict(timed_paid_contexts=len(v),sum_worker_seconds=sum(v),
            median_seconds=median(v),max_seconds=max(v)) for k,v in timings.items()},
        transport=dict(packets=len(transport),old_characters=before,new_characters=after,
            duplicated_entry_and_task_characters=sum(p['duplicated_characters'] for p in transport),
            reduction_fraction=1-after/before if before else None,all_roundtrips_equal=True,
            note='Character reduction measured offline; not a measured token/cost or behavior improvement.',packets_detail=transport),
        limits=['Role attribution for old logs is inferred; new logs record task-local actors explicitly.',
            'Historical clocks hide role wall time; this does not rank roles by measured latency.',
            'Group wall durations exist for only some completed runs; summed worker durations overlap under parallel execution.',
            'Cost uses yesterday\'s saved Standard assumptions, not account invoices.',
            'WATCH summaries may contain useful counterevidence; a repeated verdict alone does not prove uselessness.'])


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--reports',type=Path,default=Path('reports'))
    parser.add_argument('--ledger',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);a=parser.parse_args()
    result=audit(a.reports,a.ledger);a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({k:result[k] for k in ('totals','phases','actors','unique_paid_analysis_contexts')},ensure_ascii=False))


if __name__=='__main__':main()
