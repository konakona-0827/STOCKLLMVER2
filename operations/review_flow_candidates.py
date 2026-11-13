"""Real CLI + real broker reads, ending at the original pre-order risk checks.

There is deliberately no submission or simulation mode. Audit output is separate
from the running FLOW state and cannot be replayed as an executable advice file.
"""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import argparse
import json
from pathlib import Path
import time

from opportunity_flow.common import TAIPEI,policy,save,digest,stamp
from opportunity_flow.data import load_dataset
from opportunity_flow.engine import features,levels
from opportunity_flow.pipeline import run_cycle
from opportunity_flow.review import CodexRunner,validate,combine


def diagnostic_packet(c,dataset,at,p,context,event,quote=None):
    """Explain a blocked symbol without inventing an eligible BUY route."""
    bars=[b for b in dataset['grouped'][c['symbol']] if b['date']<at.date().isoformat()][-60:]
    facts=features(bars,p['holding_sessions']) if len(bars)>=21 else dict(history_sessions=len(bars))
    supports,targets=levels(bars) if bars else ([],[])
    core=dict(schema_version=1,system='TW Opportunity Flow',policy=p,symbol=c['symbol'],exchange=bars[-1]['exchange'],
        decision_session=at.date().isoformat(),completed_through=bars[-1]['date'],setup_families=[],trigger_states={},
        execution_context=context,selected_routes=[],
        decision_scope='BLOCKED_DIAGNOSTIC_ONLY_NO_ORDER_ELIGIBILITY',
        evidence=[dict(evidence_id='E_FEATURES',facts=facts),dict(evidence_id='E_LEVELS',facts=dict(supports=supports,targets=targets)),
            dict(evidence_id='E_ECONOMICS',facts=[]),dict(evidence_id='E_DATA_LIMITS',facts=dict(
                original_state=c['state'],blockers=c['reasons'],event_check=event,
                observed_oddlot_quote=deepcopy(quote),quote_observed_at=at.isoformat(),
                point_in_time_certified=False,corporate_actions_verified=False,
                note='程式已阻擋此股。本卡只解釋當下問題，沒有任何可批准的價量路徑。'))],
        recent_bars=[{k:b[k] for k in ('date','open','high','low','close','volume')} for b in bars[-8:]],
        source_history_sha256=digest(bars))
    return {**core,'packet_id':digest(core)}


class LoggedRunner(CodexRunner):
    def __init__(self,folder,*args,**kwargs):
        super().__init__(*args,**kwargs);self.folder=Path(folder);self.calls=[]
    def __call__(self,packets,role):
        started=datetime.now(TAIPEI).isoformat()
        print(json.dumps(dict(stage='CLI_START',at=started,role=role,symbols=[p['symbol'] for p in packets])),flush=True)
        try:
            result=super().__call__(packets,role)
            call=dict(started_at=started,finished_at=datetime.now(TAIPEI).isoformat(),role=role,status='VALID',symbols=[p['symbol'] for p in packets],**result)
            self.calls.append(call);save(self.folder/'calls'/f'{len(self.calls):03d}_{role}.json',call)
            print(json.dumps(dict(stage='CLI_FINISHED',role=role,symbols=call['symbols'],usage=result['usage'])),flush=True)
            return result
        except Exception as exc:
            call=dict(started_at=started,finished_at=datetime.now(TAIPEI).isoformat(),role=role,status='ERROR',
                symbols=[p['symbol'] for p in packets],usage=None,error=type(exc).__name__+':'+str(exc)[:180])
            self.calls.append(call);save(self.folder/'calls'/f'{len(self.calls):03d}_{role}.json',call)
            raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',required=True);parser.add_argument('--output',required=True);parser.add_argument('--state-root')
    parser.add_argument('--symbols',nargs='+',default=['1402','1514','3048','4904','4966','8210'])
    parser.add_argument('--model')
    args=parser.parse_args()
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    from opportunity_flow.legacy_runtime import LegacyRuntime
    from opportunity_flow.live import resolve_account_root
    from opportunity_flow.company_events import current_events,for_symbol
    from opportunity_flow.execution_bridge import verified_snapshot,validate_buy,buy_decision,payload_for
    from execution.advice_adapter import build_advice_intents
    from execution.risk_guard import quote_map_from_advice_payload,build_order_plan
    from execution.auto_advice_executor import check_refreshed_buy,check_setup_allocation
    folder=Path(args.output);folder.mkdir(parents=True,exist_ok=True)
    dataset=load_dataset(args.dataset);p=policy()
    if args.model:p['model']=args.model
    for s in args.symbols:
        if s not in dataset['grouped']:raise ValueError('SYMBOL_NOT_IN_VERIFIED_DATASET:'+s)
    source=current_events(folder/'events',datetime.now(TAIPEI))
    runtime=None;began=datetime.now(TAIPEI).isoformat()
    report=dict(started_at=began,symbols=args.symbols,actual_broker_reads=True,actual_cli=True,
        simulated_orders=0,actual_orders_sent=0,submission_calls=0,results=[])
    try:
        runtime=LegacyRuntime(resolve_account_root(args.state_root),folder/'broker',p['model'],read_only=True)
        def forbid(*unused,**kwargs):
            report['submission_calls']+=1
            raise RuntimeError('RESEARCH_AUDIT_CANNOT_SUBMIT_OR_CANCEL_ORDERS')
        runtime.execute=runtime.cancel_exit=forbid
        runtime.broker.execute=runtime.broker.execute_if_active=runtime.broker.cancel_exit=forbid
        snapshot=runtime.refresh();save(folder/'account.initial.private.json',snapshot)
        p=runtime.effective_policy(p)
        if p['minimum_commission_twd']!=1 or runtime.config.min_buy_fee_reserve_twd!=Decimal(20):
            raise ValueError('REVIEWED_FEE_OR_CASH_RESERVE_CHANGED')
        events={s:for_symbol(source,s,dataset['master'][s]['exchange'],datetime.now(TAIPEI),p['holding_sessions']) for s in args.symbols}
        runtime.disclosures=events
        quotes=runtime.fetch_quotes(args.symbols,dataset);save(folder/'quotes.initial.json',quotes)
        contexts=runtime.contexts(dataset,quotes);save(folder/'contexts.private.json',{s:contexts[s] for s in args.symbols})
        at=datetime.now(TAIPEI);save(folder/'policy.json',p)
        runner=LoggedRunner(folder,p['model'],p['model_timeout_seconds'])
        print(json.dumps(dict(stage='INITIAL_INPUT',at=at.isoformat(),budget_status=snapshot.get('budget_status'),
            buy_block_reasons=snapshot.get('buy_block_reasons'),quotes={s:q.get('quote_status') for s,q in quotes.items()})),flush=True)
        result=run_cycle(dataset,at,p,folder/'research',quotes={(q['exchange'],s):q for s,q in quotes.items()},
            events=events,runner=runner,use_llm=True,diagnose_blocked=True,review_symbols=args.symbols,
            account_context={s:contexts[s] for s in args.symbols})
        selected={c['symbol']:c for c in result['candidates'] if c['symbol'] in args.symbols}
        leftovers=[c for c in selected.values() if not c.get('review') and c['state'] not in ('REVIEW_ERROR','REVIEW_SERVICE_BACKOFF','REVIEW_DEFERRED_SERVICE_ERROR')]
        for start in range(0,len(leftovers),p['model_batch_size']):
            batch=leftovers[start:start+p['model_batch_size']]
            packets=[diagnostic_packet(c,dataset,at,p,contexts[c['symbol']],events[c['symbol']],quotes.get(c['symbol'])) for c in batch]
            answers={}
            for role in ('researcher','challenger'):
                output=runner(packets,role);answers[role]=validate(output['answer'],packets,role)
            for c,packet in zip(batch,packets):
                c['diagnostic_only']=True;c['initial_state']=c['state']
                c['review']=dict(packet=packet,researcher=answers['researcher'][packet['packet_id']],
                    challenger=answers['challenger'][packet['packet_id']],reviewed_at=datetime.now(TAIPEI).isoformat())
        save(folder/'six_research_results.json',selected)
        print(json.dumps(dict(stage='RESEARCH_FINISHED',states={s:c['state'] for s,c in selected.items()})),flush=True)
        snapshot=runtime.refresh();save(folder/'account.final.private.json',snapshot)
        quotes=runtime.fetch_quotes(args.symbols,dataset);save(folder/'quotes.final.json',quotes)
        contexts=runtime.contexts(dataset,quotes)
        for s in args.symbols:
            c=selected[s];review=c.get('review') or {}
            row=dict(symbol=s,name=dataset['master'][s]['name'],numeric_state=c.get('initial_state',c['state']),
                reasons=c.get('reasons',[]),diagnostic_only=c.get('diagnostic_only',False),
                research=review.get('researcher'),challenge=review.get('challenger'),
                account_gate='NOT_CHECKED',execution_gate='NOT_CHECKED',pretrade='NOT_REACHED',native_risk='NOT_REACHED')
            try:verified_snapshot(snapshot,datetime.now(TAIPEI));row['account_gate']='PASS'
            except ValueError as exc:row['account_gate']=str(exc)
            try:
                if not review:raise ValueError('REAL_CLI_RESEARCH_INCOMPLETE')
                route=validate_buy(c,quotes.get(s),snapshot,contexts[s],runtime.effective_policy(p),datetime.now(TAIPEI),
                    planned_limits=runtime.config.planned_limits_enabled,support_policy=runtime.support_policy)
                row['execution_gate']='PASS'
                candidate=runtime.candidates([s],quotes,dataset)[0]
                candidate.update(independent_plan_reviews=review,company_disclosures=events[s],max_buy_qty=route['economics']['quantity'],
                    flow_plan=dict(packet_id=review['packet']['packet_id'],route_id=route['route_id'],limit_cap=route['entry'],
                        quantity=route['economics']['quantity'],expected_ai_qty=contexts[s]['held_qty'],policy=p),
                    handoff_key=digest([review['packet']['packet_id'],route['route_id']]))
                decision=buy_decision(c,route,quotes[s])
                decisions,pretrade=runtime.pretrade([decision],[candidate]);save(folder/f'pretrade_{s}.json',pretrade)
                row['pretrade']=pretrade['status']
                if pretrade['status']!='VALID' or decisions[0]['decision']!='BUY':raise ValueError('PRETRADE_VETO_OR_ERROR')
                expected=buy_decision(c,route,quotes[s])
                if any(decisions[0].get(k)!=expected[k] for k in ('symbol','suggested_qty','suggested_price','target_price','invalidation_price','holding_days')):
                    raise ValueError('PRETRADE_ALTERED_FROZEN_PLAN')
                runtime.refresh();fresh=runtime.fetch_quotes([s],dataset);fresh_context=runtime.contexts(dataset,fresh)[s]
                validate_buy(c,fresh[s],runtime.snapshot,fresh_context,runtime.effective_policy(p),datetime.now(TAIPEI),
                    planned_limits=runtime.config.planned_limits_enabled,support_policy=runtime.support_policy)
                current=runtime.candidates([s],fresh,dataset)[0]
                current.update({k:candidate[k] for k in ('flow_plan','handoff_key','pricing_policy')})
                decisions[0]['reference_price']=current['last_price']
                payload=payload_for(decisions,[current],runtime.account(),datetime.now(TAIPEI),review=pretrade)
                intents=build_advice_intents(payload);book=quote_map_from_advice_payload(payload)
                from dataclasses import replace
                age=(datetime.now(TAIPEI)-stamp(current['quote_timestamp'])).total_seconds()
                quote=replace(book[s],age_seconds=Decimal(str(age)))
                plan=build_order_plan(intents[0],quote=quote,config=runtime.config.risk_config())
                risk=check_setup_allocation(quote,plan,payload['recommendations'][0],p['capital_twd'],fresh_context['held_qty'],runtime.config)
                check_refreshed_buy(quote,plan,payload['recommendations'][0],p['capital_twd'],runtime.config.economic_minimum_commission_twd,risk)
                row['native_risk']='PASS';row['reviewed_plan']=dict(quantity=plan.quantity,limit_price=str(plan.limit_price))
            except (ValueError,KeyError,TypeError,ArithmeticError) as exc:
                if row['execution_gate']=='NOT_CHECKED':row['execution_gate']=str(exc)
                else:row['subsequent_block']=str(exc)
            report['results'].append(row)
        report.update(finished_at=datetime.now(TAIPEI).isoformat(),calls=runner.calls,
            final_account=dict(checked_at=snapshot.get('checked_at'),budget_status=snapshot.get('budget_status'),buy_block_reasons=snapshot.get('buy_block_reasons')))
        save(folder/'report.json',report)
        print(json.dumps({k:report[k] for k in ('finished_at','actual_orders_sent','submission_calls','results')},ensure_ascii=False),flush=True)
    finally:
        if runtime is not None:runtime.close()


if __name__=='__main__':main()
