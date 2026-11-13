"""Review-to-executor handoff. The original broker executor remains authoritative."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import math
from pathlib import Path

from .common import TAIPEI,canonical,digest,save,stamp
from .data import validate_quote
from .engine import economics
from .review import combine,validate,verify_packet
from .state import Ledger


def verified_snapshot(snapshot,at,*,need_buying_power=True):
    if not snapshot or (need_buying_power and (snapshot.get('errors') or snapshot.get('status')=='ERROR')):
        raise ValueError('BROKER_ACCOUNT_UNVERIFIED')
    age=(stamp(at)-stamp(snapshot['checked_at'])).total_seconds()
    if not 0<=age<=120:raise ValueError('BROKER_ACCOUNT_STALE')
    if not need_buying_power:
        if snapshot.get('inventory_checked') is not True or snapshot.get('reconciliation_errors'):
            raise ValueError('BROKER_INVENTORY_UNVERIFIED')
        return
    if snapshot.get('budget_status')!='READY' or float(snapshot.get('current_buy_budget_twd') or 0)<=0:
        raise ValueError('CURRENT_ACCOUNT_ALLOWANCE_PAUSED')


def validate_buy(candidate,quote,snapshot,context,p,at,*,planned_limits=False,support_policy='advisory'):
    """Return the exact reviewed route after fresh account/book checks, not a BUY guess."""
    review=candidate.get('review') or {};packet=review.get('packet')
    if not packet:raise ValueError('FROZEN_RESEARCH_MISSING')
    verify_packet(packet)
    for role in ('researcher','challenger'):
        validate(dict(role=role,opinions=[review[role]]),[packet],role)
    state,route_id=combine(packet,review['researcher'],review['challenger'])
    if state!='RESEARCH_READY':raise ValueError('TWO_RESEARCH_APPROVALS_REQUIRED')
    if not 0<=(stamp(at)-stamp(review.get('reviewed_at'))).total_seconds()<=p['review_max_age_seconds']:
        raise ValueError('RESEARCH_REVIEW_STALE')
    if packet['decision_session']!=stamp(at).date().isoformat():raise ValueError('RESEARCH_FROM_ANOTHER_SESSION')
    verified_snapshot(snapshot,at)
    original=packet.get('execution_context') or {}
    if (original.get('source')!='LIVE_ACCOUNT_CONTEXT' or not original.get('account_identity')
            or original.get('account_identity')!=context.get('account_identity')):
        raise ValueError('LIVE_ACCOUNT_RESEARCH_REQUIRED')
    if original.get('held_qty')!=context.get('held_qty'):raise ValueError('POSITION_CHANGED_SINCE_RESEARCH')
    if context.get('pending_execution') or context.get('trading_enabled') is False or context.get('buy_block_reason'):
        raise ValueError(context.get('buy_block_reason') or 'PENDING_ORDER_OR_AI_DISABLED')
    if packet['policy']!=p:raise ValueError('EXECUTION_POLICY_CHANGED')
    q,error=validate_quote(quote,packet['symbol'],packet['exchange'],at,p['quote_max_age_seconds'])
    if error:raise ValueError(error)
    if quote.get('source_market_number')!={'TWSE':5,'TPEX':6}[packet['exchange']]:
        raise ValueError('BROKER_MARKET_NOT_VERIFIED')
    facts=packet['evidence'][3]['facts']
    if support_policy!='advisory' and not (facts.get('point_in_time_certified') and facts.get('corporate_actions_verified')):
        raise ValueError('EXISTING_STRICT_RESEARCH_POLICY_NOT_SATISFIED')
    events=facts.get('event_check') or {}
    if events.get('status') not in ('SOURCE_REVIEW_REQUIRED','CHECKED_CLEAR') or events.get('blockers'):
        raise ValueError('CURRENT_COMPANY_EVENT_CHECK_REQUIRED')
    if not events.get('source_url') or not 0<=(stamp(at)-stamp(events['checked_at'])).total_seconds()<=3600:
        raise ValueError('COMPANY_EVENT_EVIDENCE_STALE')
    route=next((r for r in packet['evidence'][2]['facts'] if r['route_id']==route_id and r['eligible']),None)
    if not route:raise ValueError('REVIEWED_ROUTE_NOT_ELIGIBLE')
    if q['bid']<=route['stop']['price'] or q['last_price']<=route['stop']['price']:
        raise ValueError('REVIEWED_PLAN_INVALIDATED')
    if quote.get('low') is not None and quote['low']<=route['stop']['price']:
        raise ValueError('REVIEWED_PLAN_INVALIDATED_THIS_SESSION')
    if q['ask']>=route['target']['price']:raise ValueError('REVIEWED_TARGET_ALREADY_REACHED')
    if not planned_limits and q['ask']>route['entry']:raise ValueError('WAITING_FOR_REVIEWED_ENTRY')
    qty=route['economics']['quantity']
    e=economics(route['entry'],route['stop']['price'],route['target']['price'],qty,q['ask']-q['bid'],p)
    oldrisk=context.get('risk_by_stop',{}).get(str(float(route['stop']['price'])))
    if context.get('held_qty',0)>0 and oldrisk is None:raise ValueError('EXISTING_POSITION_RISK_UNKNOWN')
    oldrisk=float(oldrisk or 0)
    funding=route['entry']*qty+max(float(context.get('minimum_fee_reserve_twd',0)),route['entry']*qty*float(context.get('buy_cash_buffer_rate',0)))
    allocation=p['capital_twd']*p['allocation_fraction']-int(context.get('held_qty',0))*max(q['ask'],float(context.get('average_cost_twd') or q['ask']))
    if max(funding,e['invested_twd'])>min(float(snapshot['current_buy_budget_twd']),allocation):
        raise ValueError('FRESH_CAPITAL_OR_ALLOCATION_INSUFFICIENT')
    if (e['net_profit_twd']<p['minimum_net_profit_twd'] or e['net_return']<p['minimum_net_return']
            or e['reward_risk'] is None or e['reward_risk']<p['minimum_reward_risk']
            or e['modeled_loss_twd']+oldrisk>p['capital_twd']*p['maximum_position_risk_fraction']):
        raise ValueError('FRESH_TRADE_ECONOMICS_FAILED')
    return route


def buy_decision(c,route,quote):
    p=c['review']['packet'];qty=route['economics']['quantity']
    family=p['setup_families']
    strategy='multi_day_trend' if any('TREND' in f for f in family) else 'breakout' if any('BREAKOUT' in f for f in family) else 'intraday_recovery'
    return dict(symbol=c['symbol'],decision='BUY',confidence=0,action_ratio=0,suggested_qty=qty,
        reference_price=quote['last_price'],suggested_price=route['entry'],strategy_id=strategy,
        target_price=route['target']['price'],invalidation_price=route['stop']['price'],holding_days=p['policy']['holding_sessions'],
        reason='TW Opportunity Flow 兩方通過固定計畫；'+c['review']['researcher']['thesis'],
        data_quality='LIVE',warnings=['預算與持倉按正式帳戶檢查','RAW_UNADJUSTED_LIVE_ADVISORY_NOT_PIT_CERTIFIED'])


def validate_sell(candidate,previous,decision,p,at):
    # A SELL consumes the verified bid. Missing ask must not disable an exit.
    check=candidate if candidate.get('ask') is not None else {**candidate,'ask':candidate.get('bid')}
    _,error=validate_quote(check,candidate['symbol'],candidate.get('exchange'),at,p['quote_max_age_seconds'])
    if error:raise ValueError(error)
    if candidate.get('source_market_number')!={'TWSE':5,'TPEX':6}.get(candidate.get('exchange')):
        raise ValueError('SELL_BROKER_MARKET_NOT_VERIFIED')
    if candidate.get('pending_execution'):raise ValueError('SELL_PENDING_ORDER_REQUIRES_RECONCILIATION')
    if candidate['paper_position']['qty']!=previous['paper_position']['qty']:
        raise ValueError('SELL_POSITION_CHANGED_DURING_REVIEW')
    if not 0<decision['suggested_qty']<=candidate.get('max_sell_qty',0):
        raise ValueError('SELL_EXCEEDS_FRESH_SELLABLE_QUANTITY')
    if candidate.get('ai_cost_basis')!=previous.get('ai_cost_basis'):
        raise ValueError('SELL_COST_BASIS_CHANGED_DURING_REVIEW')
    old=previous.get('exit_context') or {};new=candidate.get('exit_context') or {}
    profit_only=bool(old.get('triggers')) and all(t.startswith('POSITIVE_NET_PROFIT') or t=='ORIGINAL_TARGET_REACHED' for t in old['triggers'])
    if profit_only:
        pnl=(new.get('economics') or {}).get('estimated_realized_pnl_twd')
        if pnl is None or pnl<=0:raise ValueError('PROFIT_EXIT_CHANGED_REQUIRES_NEW_REVIEW')


def payload_for(decisions,candidates,account,at,*,kind='BUY',review=None):
    at=stamp(at);lookup={c['symbol']:c for c in candidates}
    recommendations=[];quotes=[]
    for d in decisions:
        if d['decision'] not in ('BUY','SELL'):continue
        c=lookup[d['symbol']];buy=d['decision']=='BUY';plan=c.get('flow_plan')
        decision_id='flow-'+d['decision'].lower()+':'+digest([c.get('handoff_key'),d['symbol'],d['suggested_qty']])[:40]
        row={**d,'decision_id':decision_id,'asset_type':c.get('asset_type','STOCK'),
            'analysis_source':'TW_OPPORTUNITY_FLOW','ai_managed_qty':int(c['paper_position']['qty']),
            'quote_status':c['quote_status'],'quote_timestamp':c['quote_timestamp'],
            'pricing_policy':c.get('pricing_policy','AT_QUOTE') if buy else 'AT_QUOTE',
            'buy_plan':{k:d[k] for k in ('strategy_id','target_price','invalidation_price','holding_days','reason','suggested_price')} if buy else None,
            'flow_plan':plan if buy else None,'flow_expected_ai_qty':int(c['paper_position']['qty']),
            'existing_cost_basis':c.get('ai_cost_basis'),'existing_buy_plans':c.get('buy_plans')}
        if not buy and c.get('flow_exit_action')=='TAKE_PROFIT_ORDER':
            from execution.planned_exit import contract
            row.update(pricing_policy='PLANNED_LIMIT',exit_plan=contract(c,d,at))
        if buy and row['pricing_policy']=='PLANNED_LIMIT':
            row['entry_plan']=dict(version='formal-planned-entry-v1',created_at=at.isoformat(),
                expires_at=at.replace(hour=13,minute=30,second=0,microsecond=0).isoformat(),
                limit_price=d['suggested_price'],target_price=d['target_price'],invalidation_price=d['invalidation_price'],
                reason=d['reason'],time_in_force='ROD',repricing='NEVER_AUTOMATIC')
        recommendations.append(row)
        from execution.timing import mark
        mark('handoff_quote',decision_id=decision_id,symbol=d['symbol'],side=d['decision'],
             quote_timestamp=c['quote_timestamp'],bid=c['bid'],ask=c['ask'],last_price=c['last_price'],
             quantity=d['suggested_qty'],limit_price=d['suggested_price'])
        quotes.append(dict(symbol=c['symbol'],price=c['last_price'],bid=c['bid'],ask=c['ask'],
            quote_status=c['quote_status'],exchange_time=c['quote_timestamp'],is_trial=c.get('is_trial'),
            source='CAPITAL_SKCOM',quote_basis='capital_intraday_odd_lot'))
    return dict(interface_version='1.0',run_id='tw-flow-'+digest([kind,[r['decision_id'] for r in recommendations]])[:28],
        generated_at=at.isoformat(),run_status='READY',llm_validation='VALID',real_order_sent=False,
        trading_mode='agent',pretrade_review_status='VALID',recommendations=recommendations,
        execution_budget=dict(available_cash_twd=account['available_cash_twd'],strategy_cap_twd=account['capital_limit_twd'],budget_status=account['budget_status']),
        market=dict(source='CAPITAL_SKCOM',quote_basis='capital_intraday_odd_lot',quotes=quotes),
        flow_pretrade_evidence=review,orders_already_sent=False)


class ExecutionBridge:
    def __init__(self,runtime,state,p,clock=None):
        self.runtime,self.state,self.policy=runtime,Path(state),p
        self.now=clock or (lambda:datetime.now(TAIPEI))

    def dispatch(self,payload):
        if self.runtime.stopped():return dict(status='USER_STOPPED',results=[],broker_order_sent=False)
        if not self.runtime.can_trade_now():return dict(status='OUTSIDE_TRADING_SESSION',results=[],broker_order_sent=False)
        ledger=Ledger(self.state)
        try:
            ids=[r['decision_id'] for r in payload['recommendations']]
            if not ids:return dict(status='NO_TRADE_ACTION',results=[])
            if self.runtime.read_only:return dict(status='READ_ONLY',results=[],broker_order_sent=False)
            if not ledger.claim_handoff(ids,payload):return dict(status='SKIPPED_DUPLICATE_HANDOFF',results=[])
            path=self.state/'handoffs'/(payload['run_id']+'.json')
            try:
                save(path,payload)
                report=self.runtime.execute(payload,path)
            except Exception as exc:
                report=dict(status='DISPATCH_UNKNOWN_DO_NOT_RETRY',error=type(exc).__name__+':'+str(exc)[:180],automatic_retry=False)
            ledger.finish_handoff(ids,report)
            save(path.with_suffix('.execution.json'),report)
            return report
        finally:ledger.close()

    def review_positions(self,dataset,at,reports,blocked):
        runtime=self.runtime
        runtime.refresh()
        held={s:q for s,q in runtime.store.ai_positions().items() if q>0}
        candidates=[]
        # Exit review is independent of the new-buy shortlist and its data gaps.
        if held:
            verified_snapshot(runtime.snapshot,self.now(),need_buying_power=False)
            quotes=runtime.fetch_quotes(list(held),dataset)
            candidates=runtime.candidates(list(held),quotes,dataset)
            candidates=[c for c in candidates if c.get('quote_status')=='LIVE' and not c.get('is_trial') and
                ((c.get('max_sell_qty',0)>0 and not c.get('pending_execution')) or
                 (c.get('pending_exit') and not c['pending_exit'].get('cancel_requested')))]
        for candidate in candidates:
            if runtime.stopped():break
            try:self.review_position(candidate,dataset,at,reports,blocked)
            except Exception as exc:
                blocked.append(dict(symbol=candidate['symbol'],reason='POSITION_REVIEW_ERROR:'+type(exc).__name__+':'+str(exc)[:180]))

    def review_position(self,candidate,dataset,at,reports,blocked):
        runtime=self.runtime;candidates=[candidate]
        key=digest([{k:c.get(k) for k in ('symbol','paper_position','max_sell_qty','ai_cost_basis','buy_plans','user_handoff','pending_exit','company_disclosures')}|
            dict(exit_triggers=(c.get('exit_context') or {}).get('triggers',[]))|
            dict(price_band=math.floor(math.log(c['last_price'])/math.log(1.003))) for c in candidates])
        ledger=Ledger(self.state)
        try:
            now=stamp(self.now()).timestamp()
            saved=ledger.lookup('exit:'+key,now,{**self.policy,'review_max_age_seconds':3600})
            plans=saved['data']['response'].get('management_plans',[]) if saved and saved['status']=='VALID' else []
            by_symbol={c['symbol']:c for c in candidates}
            wake=any((p.get('watch_above') is not None and by_symbol[p['symbol']]['bid']>=p['watch_above']) or
                (p.get('watch_below') is not None and by_symbol[p['symbol']]['bid']<=p['watch_below']) for p in plans)
            ttl=min([p.get('next_review_seconds',300) for p in plans] or [300])
            if saved and saved['status']=='VALID' and now-saved['updated']<ttl and not wake:
                exit_result,response=saved['data']['result'],saved['data']['response']
            else:
                exit_result,response=runtime.review_exits(candidates)
                if response['validation']!='VALID':raise ValueError('EXISTING_EXIT_REVIEW_ERROR')
                ledger.finish('exit:'+key,'VALID',stamp(self.now()).timestamp(),dict(result=exit_result,response=response))
        finally:ledger.close()
        blocked.extend(dict(symbol=e['symbol'],reason='POSITION_MODEL_ERROR:'+e['error']) for e in response.get('errors',[]))
        for cancellation in response.get('cancellations',[]):
            reports.append(runtime.cancel_exit(cancellation['decision_id']))
        sells=[deepcopy(d) for d in exit_result['decisions'] if d['decision']=='SELL']
        if sells:
            from execution.timing import current_trace,mark
            if current_trace() is not None:current_trace().enable(getattr(runtime,'root',self.state))
            for decision in sells:
                mark('sell_proposal',symbol=decision['symbol'],side='SELL',quote_timestamp=candidate['quote_timestamp'],
                     bid=candidate.get('bid'),ask=candidate.get('ask'))
            fresh=runtime.fetch_quotes([d['symbol'] for d in sells],dataset)
            runtime.refresh();verified_snapshot(runtime.snapshot,self.now(),need_buying_power=False)
            current=runtime.candidates([d['symbol'] for d in sells],fresh,dataset)
            valid=[]
            previous={c['symbol']:c for c in candidates}
            for c in current:
                d=next(d for d in sells if d['symbol']==c['symbol'])
                try:validate_sell(c,previous[c['symbol']],d,self.policy,self.now())
                except ValueError as exc:
                    blocked.append(dict(symbol=c['symbol'],reason=str(exc)));continue
                c['handoff_key']=key
                action=next((p['action'] for p in response.get('management_plans',[]) if p['symbol']==c['symbol']),'SELL')
                c['flow_exit_action']=action
                if action=='TAKE_PROFIT_ORDER':
                    draft=(c.get('exit_context') or {}).get('resting_order')
                    if not draft or draft['limit_price']!=d['suggested_price']:continue
                else:d['suggested_price']=c.get('bid')
                d['reference_price']=c['last_price']
                valid.append(d)
            final,review=runtime.pretrade(valid,current)
            if review['status'] in ('VALID','NOT_NEEDED'):
                approved=[d for d in final if d['decision']=='SELL']
                if approved:
                    newest_quotes=runtime.fetch_quotes([d['symbol'] for d in approved],dataset)
                    runtime.refresh();verified_snapshot(runtime.snapshot,self.now(),need_buying_power=False)
                    newest=runtime.candidates([d['symbol'] for d in approved],newest_quotes,dataset)
                    current_by_symbol={c['symbol']:c for c in current};send=[];send_candidates=[]
                    for c in newest:
                        d=next(d for d in approved if d['symbol']==c['symbol'])
                        previous_c=current_by_symbol[c['symbol']]
                        try:validate_sell(c,previous_c,d,self.policy,self.now())
                        except ValueError as exc:
                            blocked.append(dict(symbol=c['symbol'],reason=str(exc)));continue
                        c['handoff_key']=key;c['flow_exit_action']=previous_c['flow_exit_action']
                        if c['flow_exit_action']=='TAKE_PROFIT_ORDER':c['exit_context']=previous_c['exit_context']
                        else:d['suggested_price']=c['bid']
                        d['reference_price']=c['last_price'];send.append(d);send_candidates.append(c)
                    if send:reports.append(self.dispatch(payload_for(send,send_candidates,runtime.account(),self.now(),kind='SELL',review=review)))

    def step(self,result,dataset,at):
        from execution.timing import trace_scope,span
        with trace_scope() as trace:
            with span('flow_cycle'):
                report=self._step(result,dataset,at)
            report['execution_timing']=trace.export()
            return report

    def _step(self,result,dataset,at):
        runtime=self.runtime;reports=[];blocked=[]
        if runtime.stopped() or not runtime.can_trade_now():
            return dict(reports=[],blocked=[dict(reason='USER_STOPPED_OR_OUTSIDE_TRADING_SESSION')])
        try:self.review_positions(dataset,at,reports,blocked)
        except Exception as exc:
            blocked.append(dict(reason='POSITION_REVIEW_ERROR:'+type(exc).__name__+':'+str(exc)[:180]))
        runtime.refresh()
        try:p=runtime.effective_policy(self.policy)
        except ValueError as exc:return dict(reports=reports,blocked=[dict(reason=str(exc))])
        for candidate in result['candidates']:
            if candidate['state']!='RESEARCH_READY':continue
            symbol=candidate['symbol']
            try:
                quotes=runtime.fetch_quotes([symbol],dataset)
                context=runtime.contexts(dataset,quotes)[symbol]
                route=validate_buy(candidate,quotes.get(symbol),runtime.snapshot,context,p,self.now(),
                    planned_limits=runtime.config.planned_limits_enabled,support_policy=runtime.support_policy)
                c=runtime.candidates([symbol],quotes,dataset)[0]
                c.update(flow_plan=dict(packet_id=candidate['review']['packet']['packet_id'],route_id=route['route_id'],
                    limit_cap=route['entry'],policy=p,expected_ai_qty=context['held_qty'],
                    quantity=route['economics']['quantity'],
                    existing_modeled_risk_twd=route.get('existing_modeled_risk_twd',0)),
                    handoff_key=digest([candidate['review']['packet']['packet_id'],route['route_id']]),
                    max_buy_qty=route['economics']['quantity'],
                    pricing_policy='PLANNED_LIMIT' if runtime.config.planned_limits_enabled else 'AT_QUOTE',
                    independent_plan_reviews=candidate['review'],company_disclosures=candidate['review']['packet']['evidence'][3]['facts']['event_check'])
                decision=buy_decision(candidate,route,quotes[symbol])
                from execution.timing import current_trace,mark
                if current_trace() is not None:current_trace().enable(getattr(runtime,'root',self.state))
                mark('buy_proposal',symbol=symbol,side='BUY',research_reviewed_at=candidate['review']['reviewed_at'],
                     quote_timestamp=quotes[symbol]['quote_timestamp'],bid=quotes[symbol]['bid'],ask=quotes[symbol]['ask'])
                decisions,review=runtime.pretrade([decision],[c])
                if review['status']!='VALID' or decisions[0]['decision']!='BUY':
                    blocked.append(dict(symbol=symbol,reason='PRETRADE_VETO_OR_ERROR'));continue
                expected=buy_decision(candidate,route,quotes[symbol])
                if any(decisions[0].get(k)!=expected[k] for k in ('symbol','suggested_qty','suggested_price','target_price','invalidation_price','holding_days')):
                    blocked.append(dict(symbol=symbol,reason='PRETRADE_ALTERED_FROZEN_PLAN'));continue
                runtime.refresh();quotes=runtime.fetch_quotes([symbol],dataset)
                context=runtime.contexts(dataset,quotes)[symbol]
                validate_buy(candidate,quotes.get(symbol),runtime.snapshot,context,runtime.effective_policy(self.policy),self.now(),
                    planned_limits=runtime.config.planned_limits_enabled,support_policy=runtime.support_policy)
                current=runtime.candidates([symbol],quotes,dataset)[0]
                current.update({k:c[k] for k in ('flow_plan','handoff_key','pricing_policy')})
                decisions[0]['reference_price']=current['last_price']
                reports.append(self.dispatch(payload_for(decisions,[current],runtime.account(),self.now(),review=review)))
                break  # Next BUY uses a newly reconciled account, not this budget snapshot.
            except (ValueError,KeyError,TypeError,ArithmeticError) as exc:
                blocked.append(dict(symbol=symbol,reason=str(exc)))
        return dict(reports=reports,blocked=blocked,account_checked_at=(runtime.snapshot or {}).get('checked_at'))
