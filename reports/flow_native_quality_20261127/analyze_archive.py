"""Recalculate the Nov 26 audit solely from committed, frozen live evidence."""
from collections import Counter,defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'reports/trading_daily/2026-11-26/postclose_140011'


def analyze():
    names=['summary.json','research_decisions.json','cycles_and_feed.json','execution_and_health.json','recovery_events.json']
    data={n:json.loads((SOURCE/n).read_text(encoding='utf-8')) for n in names}
    summary=data['summary.json'];research=data['research_decisions.json'];pairs=research['paired_reviews'];calls=research['calls']
    assert summary['trade_date']=='2026-11-26'
    assert len({p['packet_id'] for p in pairs})==len(pairs)
    cycles=data['cycles_and_feed.json']['cycles'];events=data['recovery_events.json']['events']
    by_symbol=defaultdict(list)
    for pair in pairs:by_symbol[pair['symbol']].append(pair)
    crashes=[e for e in events if e['event']=='FLOW_EXITED' and e.get('return_code',e.get('exit_code'))==0xC0000005]
    alarms=[e for e in events if e['event']=='MEMORY_PRESSURE_DETECTED']
    # Keep the journal's observed event fields rather than assign a native
    # faulting module absent from the archived dump/stack evidence.
    return dict(source_files={str((SOURCE/n).relative_to(ROOT)).replace('\\','/'):hashlib.sha256((SOURCE/n).read_bytes()).hexdigest() for n in names},
        cutoff_taipei=summary['cutoff_taipei'],cutoff_japan=summary['cutoff_japan'],
        model_calls=len(calls),call_statuses=dict(Counter(c['status'] for c in calls)),
        completed_pairs=len(pairs),pair_states=dict(Counter(p['state'] for p in pairs)),unique_reviewed_symbols=len(by_symbol),
        per_symbol={symbol:dict(pairs=len(rows),states=dict(Counter(r['state'] for r in rows)),
            received_completed_minutes=[r['history']['completed_minute_rows'] for r in rows],
            target_required_returns=[route['target_required_return'] for r in rows for route in r['economics'] if route.get('eligible')],
            system_gap_reported_by_models=any('SYSTEM_DATA_GAP' in text for r in rows for role in ('researcher','challenger')
                for text in r[role].get('execution_missing',[]))) for symbol,rows in sorted(by_symbol.items())},
        errors=[dict(at=c['at'],role=c['role'],symbols=c['symbols'],error=c.get('error')) for c in calls if c['status']=='ERROR'],
        cycle_count=len(cycles),max_states_across_cycles={state:max(c['states'].get(state,0) for c in cycles)
            for state in sorted({s for c in cycles for s in c['states']})},
        last_cycle=dict(at=cycles[-1]['as_of'],states=cycles[-1]['states']),
        formal_trading=summary['formal_trading'],memory_alarms=len(alarms),access_violations=crashes,
        event_counts=dict(Counter(e['event'] for e in events)),
        conclusions=dict(market_opportunity_absence_verified=False,all_nontrades_proven_rational=False,
            native_faulting_module_verified=False,complete_counterfactual_quote_path_available=False,
            observed_system_interruptions=len([c for c in calls if c['status']=='ERROR']),
            known_event_repeated_research_symbol='3706',
            historical_action_adjustment='SYSTEM_NOT_INTEGRATED_NOT_MARKET_ABSENCE'))


if __name__=='__main__':
    output=Path(__file__).with_name('archive_analysis.json')
    result=analyze();output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k in ('model_calls','completed_pairs','pair_states','memory_alarms','access_violations')},ensure_ascii=False))
    today=ROOT/'reports/trading_daily/2026-11-27/intraday_113225'
    names=['summary.json','ready_plan_handoff.json','research_decisions.json','recovery_and_execution_stages.json']
    saved={n:json.loads((today/n).read_text(encoding='utf-8')) for n in names}
    summary=saved['summary.json'];handoff=saved['ready_plan_handoff.json'];recovery=saved['recovery_and_execution_stages.json']
    ready=handoff['ready_reviews'];unexplained=[]
    for r in ready:
        next_at=min((x['at'] for x in ready if x['symbol']==r['symbol'] and x['at']>r['at']),default='9999')
        if not any(x['symbol']==r['symbol'] and r['at']<=x['at']<next_at for x in handoff['pretrade_reviews']):
            unexplained.append(dict(packet_id=r['packet_id'],symbol=r['symbol'],at=r['at'],
                conclusion='NO_DURABLE_PRETRADE_OR_STOP_REASON_IN_THIS_ARCHIVE'))
    report=dict(source_files={str((today/n).relative_to(ROOT)).replace('\\','/'):hashlib.sha256((today/n).read_bytes()).hexdigest() for n in names},
        cutoff_taipei=summary['cutoff_taipei'],cutoff_japan=summary['cutoff_japan'],model=summary['model'],
        health=summary['health'],formal_trading=summary['formal_trading'],
        pretrade_decisions=[dict(at=r['at'],symbol=r['symbol'],approved=r['approved']) for r in handoff['pretrade_reviews']],
        early_ready_paths_without_durable_reason=unexplained,
        memory_exceeds=recovery['memory_exceeds'],memory_exceeds_during_market=recovery['memory_exceeds_during_market'],
        recovery_event_counts=dict(Counter(e['event'] for e in recovery['events'])),
        exit_code_counts=dict(Counter(str(e.get('exit_code')) for e in recovery['events'] if e['event']=='FLOW_EXITED')),
        access_violations=[e for e in recovery['events'] if e['event']=='FLOW_EXITED' and e.get('exit_code')==0xC0000005],
        horizon_anchor_comparisons=[dict(symbol='2338',entry=33.35,target=39.2,previous_close_cited_in_review=35.3,
            required_from_entry=39.2/33.35-1,from_cited_previous_close=39.2/35.3-1),
            dict(symbol='3034',entry=426,target=467.5,previous_close_cited_in_review=446,
            required_from_entry=467.5/426-1,from_cited_previous_close=467.5/446-1)],
        conclusions=dict(real_research_to_broker_acknowledgement_observed=True,confirmed_fill_observed=False,
            actual_post_fill_position_management_verified_today=False,all_decisions_proven_correct=False,
            full_intraday_quote_path_for_missed_opportunity_replay_available=False))
    Path(__file__).with_name('today_analysis.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(today_submissions=summary['formal_trading']['submissions'],
        confirmed_filled_shares=summary['formal_trading']['broker_verified_filled_shares'],
        ready_paths_without_durable_reason=len(unexplained),memory_exceeds=recovery['memory_exceeds'])))
