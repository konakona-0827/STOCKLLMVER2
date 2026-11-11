"""One-symbol position agent, independent of discovery and its hourly schedule."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
from threading import RLock, Thread
import uuid

from config import CODE_ROOT, now, redact
from main import save_json


def policy():
    p = json.loads((CODE_ROOT/'config/position_manager.json').read_text('utf-8'))
    if (type(p['enabled']) is not bool or
            not 60 <= p['minimum_review_seconds'] <= p['default_review_seconds'] <= p['maximum_review_seconds'] <= 3600):
        raise ValueError('INVALID_POSITION_MANAGER_POLICY')
    return p


def plan_request(candidate, account, previous=None):
    from candidate_analyzer import object_schema
    from trading_agent import agent_model
    p = policy()
    schema = object_schema(dict(
        symbol=dict(type='string'),
        action=dict(type='string',enum=['HOLD','SELL','TAKE_PROFIT_ORDER','CANCEL_EXIT_AND_REVIEW','RESEARCH_ADD']),
        sell_quantity=dict(type='integer',minimum=0,maximum=999),
        confidence=dict(type='number',minimum=0,maximum=1),
        reason=dict(type='string'),
        next_review_seconds=dict(type='integer',minimum=p['minimum_review_seconds'],maximum=p['maximum_review_seconds']),
        watch_above=dict(type=['number','null']),watch_below=dict(type=['number','null']),
        required_data=dict(type='array',items=dict(type='string',enum=['LIVE_ODDLOT','DAILY_STRUCTURE','RECENT_OBSERVATIONS']))))
    instructions = '''你是持倉管理子代理，每次只負責輸入的一檔 AI 持倉，其他代理繼續尋找新股票。
用最新可成交零股買價、成本、原計畫、完成日線和真實觀測，決定續抱、賣出、預掛停利或研究加倉。
只要扣除雙邊費用與賣出稅後仍有正收益，且已有具體回落、近端壓力或動能轉弱證據，
可提前賣出，不必等原目標；單次跌價不等於已形成轉折。不要把普通交易 K 線當成零股成交證明。
風險退出可虧損；max_sell_qty=0、人工持股、未知成本或未結單不可冒充可獲利成交。
TAKE_PROFIT_ORDER 僅使用 exit_context.resting_order 的價量；預掛不是已成交。
有 pending_exit 時只可 HOLD 或 CANCEL_EXIT_AND_REVIEW；具體風險或轉弱時可撤掉本系統預掛賣單。
撤單後必須等待券商確認撤單與最後成交，再重新推論剩餘股數；不可同時再賣一次。
RESEARCH_ADD 只是請求原雙方研究及數字風控，不得逢跌加碼、提高配置或跳過原檢查。
HOLD/RESEARCH_ADD 的 sell_quantity=0；SELL/TAKE_PROFIT_ORDER 不得超過 max_sell_qty。
說明下次多久後觀察及需要什麼資料；有獲利接近壓力或可能失效時縮短，平穩續抱可延長。
watch_below 必須低於當前 bid，watch_above 必須高於當前 bid；它們是提前喚醒條件，不是預測。
confidence 沿用既有主觀可信度欄位，不是預測勝率。
不得填入未來行情或只因持有期限到期就強制賣出。以繁體中文說明具體證據。'''
    return dict(model=agent_model(),store=False,instructions=instructions,
        input=json.dumps(dict(candidate=candidate,account=account,previous_management=previous),ensure_ascii=False),
        text=dict(format=dict(type='json_schema',name='position_management',strict=True,schema=schema)),
        max_output_tokens=None)


def validate_plan(plan, candidate):
    p = policy()
    if (plan.get('symbol') != candidate['symbol'] or plan.get('action') not in
            ('HOLD','SELL','TAKE_PROFIT_ORDER','CANCEL_EXIT_AND_REVIEW','RESEARCH_ADD') or not isinstance(plan.get('reason'),str)
            or not plan['reason'].strip() or type(plan.get('next_review_seconds')) is not int
            or not p['minimum_review_seconds'] <= plan['next_review_seconds'] <= p['maximum_review_seconds']):
        raise ValueError('INVALID_POSITION_MANAGEMENT_PLAN')
    quantity = plan.get('sell_quantity')
    if type(quantity) is not int or quantity < 0:
        raise ValueError('INVALID_POSITION_MANAGEMENT_QUANTITY')
    if plan['action'] in ('SELL','TAKE_PROFIT_ORDER'):
        if not 0 < quantity <= min(999,int(candidate.get('max_sell_qty') or 0)):
            raise ValueError('POSITION_MANAGEMENT_EXCEEDS_SELLABLE')
        if plan['action']=='TAKE_PROFIT_ORDER':
            draft=(candidate.get('exit_context') or {}).get('resting_order')
            if not draft or quantity != draft['quantity']:
                raise ValueError('NO_REVIEWED_RESTING_EXIT')
    elif quantity != 0:
        raise ValueError('NON_SELL_MANAGEMENT_QUANTITY')
    if plan['action']=='CANCEL_EXIT_AND_REVIEW' and not candidate.get('pending_exit'):
        raise ValueError('NO_OWN_PENDING_EXIT_TO_CANCEL')
    if candidate.get('pending_execution') and plan['action'] not in ('HOLD','CANCEL_EXIT_AND_REVIEW'):
        raise ValueError('PENDING_EXIT_MUST_BE_CONFIRMED_BEFORE_REPLACEMENT')
    from math import isfinite
    confidence=plan.get('confidence')
    if type(confidence) not in (int,float) or not isfinite(confidence) or not 0<=confidence<=1:
        raise ValueError('INVALID_POSITION_CONFIDENCE')
    for key,direction in [('watch_above',1),('watch_below',-1)]:
        value = plan.get(key)
        if value is not None and (type(value) not in (int,float) or not isfinite(value)
                or value <= 0 or direction*(value-float(candidate['bid'])) <= 0):
            raise ValueError('INVALID_POSITION_WAKE_LEVEL')
    if (not isinstance(plan.get('required_data'),list) or not plan['required_data'] or
            set(plan['required_data'])-{'LIVE_ODDLOT','DAILY_STRUCTURE','RECENT_OBSERVATIONS'}):
        raise ValueError('INVALID_POSITION_DATA_REQUIREMENTS')
    plan['required_data'] = list(dict.fromkeys(['LIVE_ODDLOT',*plan['required_data']]))
    return plan


class PositionSupervisor:
    """Health snapshots wake a separate worker; never run a model on Tk/COM threads."""
    def __init__(self, directory, reviewer, *, clock=now):
        self.directory, self.reviewer, self.clock = Path(directory), reviewer, clock
        self.path = self.directory/'state.json'
        self.lock, self.active, self.busy = RLock(), False, False
        self.observations, self.states = {}, {}
        try:
            self.states = json.loads(self.path.read_text('utf-8'))
        except (OSError,ValueError):
            pass
        if not isinstance(self.states,dict):
            self.states = {}

    def set_active(self, active):
        with self.lock:
            self.active = bool(active and policy()['enabled'])

    def summary(self):
        with self.lock:
            if not self.active:
                return '未啟用'
            rows=[]
            for symbol,state in sorted(self.states.items()):
                try:
                    due=datetime.fromisoformat(state['next_review_at']).strftime('%H:%M:%S')
                except (KeyError,ValueError):
                    due='待確認'
                rows.append(f'{symbol} 下次 {due}'+('（待重試）' if state.get('error') else ''))
            return ('推論中；' if self.busy else '')+('；'.join(rows) or '等待 AI 持倉')

    def observe(self, snapshot):
        from scheduler import is_twse_oddlot_market_window
        from math import isfinite
        at = self.clock()
        with self.lock:
            if not self.active or not is_twse_oddlot_market_window(at.timestamp()):
                return []
            if snapshot.get('broker_inventory_verified') is not True:
                return []
            quantities = snapshot.get('ai_positions') or {}
            settings = snapshot.get('ai_position_settings') or {}
            quotes = {q['symbol']:q for q in snapshot.get('position_quotes') or []}
            live = {s:int(q) for s,q in quantities.items() if int(q)>0 and settings.get(s,{}).get('enabled',True)}
            for symbol in set(self.states)-set(live):
                self.states.pop(symbol,None)
                self.observations.pop(symbol,None)
            due = []
            for symbol,qty in live.items():
                quote = quotes.get(symbol) or {}
                try:
                    stamp = datetime.fromisoformat(quote['quote_timestamp'])
                    if (stamp.tzinfo is None or not 0 <= (at-stamp).total_seconds() <= 120
                            or quote.get('quote_status')!='LIVE' or quote.get('is_trial') is not False
                            or any(not isfinite(float(quote[key])) or float(quote[key])<=0 for key in ('bid','last_price'))
                            or (quote.get('ask') is not None and (not isfinite(float(quote['ask']))
                                or float(quote['bid'])>float(quote['ask'])))):
                        continue
                except (KeyError,ValueError,TypeError):
                    continue
                history = self.observations.setdefault(symbol,[])
                if not history or history[-1]['exchange_time'] != stamp.isoformat():
                    history.append(dict(symbol=symbol,exchange_time=stamp.isoformat(),price=quote['last_price'],
                        quote_status='LIVE',is_trial=False,retrieval_mode='BROKER_HEALTH'))
                    del history[:-20]
                state = self.states.get(symbol) or {}
                reason = None
                try:
                    review_at=datetime.fromisoformat(state['next_review_at'])
                    last_at=datetime.fromisoformat(state['reviewed_at'])
                    if (review_at.tzinfo is None or last_at.tzinfo is None or last_at>at
                            or not 0<=(review_at-last_at).total_seconds()<=policy()['maximum_review_seconds']):
                        raise ValueError('INVALID_SAVED_POSITION_SCHEDULE')
                    if state.get('quantity')!=qty:
                        reason='FILLED_POSITION_CHANGED'
                    elif at>=review_at:
                        reason='AGENT_REVIEW_DUE'
                    elif (at-last_at).total_seconds()>=policy()['minimum_review_seconds']:
                        price=float(quote['bid']);plan=state.get('plan') or {}
                        if ((plan.get('watch_above') is not None and price>=plan['watch_above'])
                                or (plan.get('watch_below') is not None and price<=plan['watch_below'])):
                            reason='PRICE_WAKE_LEVEL_TOUCHED'
                        if state.get('invalidation_price') is not None and price<=state['invalidation_price']:
                            reason='ORIGINAL_INVALIDATION_TOUCHED'
                        if state.get('target_price') is not None and price>=state['target_price']:
                            reason='ORIGINAL_TARGET_REACHED'
                        latest=history[-3:]
                        if (len(latest)==3 and latest[0]['price']>latest[1]['price']>latest[2]['price']
                                and latest[-1]['price']<=max(r['price'] for r in history)*.995):
                            reason='OBSERVED_PROFIT_GIVEBACK_REQUIRES_REVIEW'
                except (KeyError,ValueError,TypeError):
                    reason='NEW_POSITION_OR_UNAVAILABLE_SCHEDULE'
                if reason:
                    due.append((symbol,reason,qty))
            if self.busy or not due:
                return []
            # Oldest due first so a rapidly changing symbol cannot starve others.
            due.sort(key=lambda item:(self.states.get(item[0],{}).get('reviewed_at',''),item[0]))
            symbol,reason,qty = due[0]
            self.busy = True
            state=deepcopy(self.states.get(symbol) or {})
            observations=deepcopy(self.observations.get(symbol) or [])
            Thread(target=self._work,args=(symbol,reason,qty,state,observations),
                   name='position-manager-'+symbol,daemon=True).start()
            return [symbol]

    def _work(self, symbol, reason, qty, previous, observations):
        at=self.clock()
        try:
            result=self.reviewer(symbol,previous,observations,lambda:not self.active)
            plan=result['management_plan']
            delay=plan['next_review_seconds']
            if type(delay) is not int or not policy()['minimum_review_seconds']<=delay<=policy()['maximum_review_seconds']:
                raise ValueError('INVALID_NEXT_POSITION_REVIEW_TIME')
            limits=result.get('original_exit_levels') or {}
            state=dict(quantity=qty,reviewed_at=self.clock().isoformat(),
                next_review_at=(self.clock()+timedelta(seconds=delay)).isoformat(),
                plan=plan,run_id=result['manifest']['run_id'],wake_reason=reason,**limits)
        except Exception as exc:
            state=dict(quantity=qty,reviewed_at=at.isoformat(),
                next_review_at=(self.clock()+timedelta(seconds=60)).isoformat(),
                error=redact(f'{type(exc).__name__}: {exc}'),wake_reason=reason)
        finally:
            with self.lock:
                self.states[symbol]=state
                self.directory.mkdir(parents=True,exist_ok=True)
                try:
                    save_json(self.path,self.states)
                finally:
                    self.busy=False


def review_position(service, symbol, previous, observations, stopped, *, provider=None):
    """Fresh single-position inference; existing reviewers and executor own orders."""
    from decimal import Decimal
    from types import SimpleNamespace
    from analysis_service import (ROOT, live_ai_positions, build_union, apply_live_position_context,
                                  apply_buy_limits, live_buy_budget)
    from execution.auto_advice_executor import LiveExecutionConfig
    from execution.broker_runtime import get_broker_runtime
    from execution.execution_store import ExecutionStore
    from execution.health_monitor import sync_broker_state
    from main import Audit
    from universe import load_universe,load_rules
    import daily_history, paper_portfolio, position_analyzer, position_store, strategy_catalog
    from opportunity_engine import refresh as refresh_opportunity
    from profit_exit import refresh as refresh_exit
    from candidate_analyzer import call_openai
    from advice_interface import write_advice_interface
    from capital_multi_quote import multi_snapshot

    def check_stop():
        if stopped() or service.stop.is_set():
            raise ValueError('POSITION_MANAGER_STOPPED')

    check_stop()
    run_id=now().strftime('%Y%m%dT%H%M%S')+'-position-'+symbol+'-'+uuid.uuid4().hex[:8]
    directory=service.directory/'position_manager'/'runs'/run_id
    directory.mkdir(parents=True)
    audit=Audit(directory)
    rules=LiveExecutionConfig.load(ROOT/'config/execution_live.json')
    if not rules.enabled:
        raise ValueError('POSITION_MANAGER_REQUIRES_EXISTING_LIVE_AUTHORIZATION')
    health,_=sync_broker_state(ROOT,return_report=True)
    execution_db=ROOT/'data/execution/live_execution.sqlite3'
    positions=live_ai_positions(execution_db)
    held=[p for p in positions if p['symbol']==symbol and p['qty']>0]
    if not held or (health.get('ai_position_settings') or {}).get(symbol,{}).get('enabled') is False:
        raise ValueError('POSITION_NO_LONGER_AI_MANAGED')
    store=ExecutionStore(execution_db,must_exist=True)
    capital=Decimal(paper_portfolio.account(service.db_path)['capital_limit_cents'])/100
    budget=live_buy_budget(health,store.daily_reserved_buy_twd(),capital,rules.max_daily_buy_twd)
    account=dict(capital_limit_twd=float(capital),available_cash_cents=int(budget['available']*100),
        available_cash_twd=float(budget['available']),budget_status=budget['status'],positions=positions,
        source='CONFIGURED_CAP_REMAINING_WITH_BROKER_FUNDS_AND_RESERVE_CHECKS')
    check_stop()
    started=now().isoformat()
    raw,errors=get_broker_runtime(ROOT).fetch_quotes([symbol],audit,lambda:stopped() or service.stop.is_set())
    batch=multi_snapshot([symbol],raw,errors,started,now().isoformat())
    universe=load_universe(ROOT/'config/universe_tw.json')
    candidates=build_union([],[],batch['quotes'],position_store.load(service.db_path),service.db_path,started,
        etf_symbols=universe.get('etf_symbols',[]),paper_positions=held,live_managed=True,
        ai_settings=health.get('ai_position_settings'))
    if len(candidates)!=1:
        raise ValueError('SINGLE_AI_POSITION_REQUIRED')
    c=candidates[0]
    c['market_history']=list({r['exchange_time']:r for r in [*(c.get('market_history') or []),*observations]}.values())
    from target_reachability import policy as reachability_policy
    daily_history.enrich_candidates(candidates,{q['symbol']:q for q in batch['quotes']},now().date(),
        service.directory/'daily_history',max_bars=reachability_policy()['history_bars'],verify_sessions=True)
    apply_live_position_context(candidates,health,execution_db,audit)
    apply_buy_limits(candidates,account,rules)
    c['pending_execution']=symbol in store.pending_reconciliation_symbols()
    from execution.exit_cancel import pending_exit
    c['pending_exit']=pending_exit(store,symbol) if c['pending_execution'] else None
    refresh_opportunity(c,account,rules)
    refresh_exit(c,rules)
    if (c.get('quote_status')!='LIVE' or c.get('is_trial') is not False
            or (c['pending_execution'] and (not c['pending_exit'] or c['pending_exit']['cancel_requested']))):
        raise ValueError('POSITION_QUOTE_OR_PENDING_ORDER_UNAVAILABLE')
    request=plan_request(c,account,previous)
    save_json(directory/'management_request.json',request)
    check_stop()
    raw_plan,usage=(provider or call_openai)(request)
    plan=validate_plan(json.loads(raw_plan),c)
    save_json(directory/'management_response.json',dict(plan=plan,api=usage))
    check_stop()
    # Inference never proves that the old quote/ownership remains executable.
    fresh_health,_=sync_broker_state(ROOT,return_report=True)
    fresh_held=next((p for p in live_ai_positions(execution_db) if p['symbol']==symbol),{})
    if fresh_held.get('qty')!=c['paper_position']['qty']:
        raise ValueError('POSITION_CHANGED_DURING_MANAGEMENT_INFERENCE')
    raw,errors=get_broker_runtime(ROOT).fetch_quotes([symbol],audit,lambda:stopped() or service.stop.is_set())
    fresh_batch=multi_snapshot([symbol],raw,errors,now().isoformat(),now().isoformat())
    c.update(fresh_batch['quotes'][0])
    apply_live_position_context(candidates,fresh_health,execution_db,audit)
    apply_buy_limits(candidates,account,rules)
    c['pending_execution']=symbol in store.pending_reconciliation_symbols()
    c['pending_exit']=pending_exit(store,symbol) if c['pending_execution'] else None
    refresh_opportunity(c,account,rules)
    refresh_exit(c,rules)
    if (c.get('quote_status')!='LIVE' or c.get('is_trial') is not False
            or (c['pending_execution'] and (not c['pending_exit'] or c['pending_exit']['cancel_requested']))):
        raise ValueError('POSITION_CHANGED_OR_QUOTE_UNAVAILABLE_AFTER_INFERENCE')
    validate_plan(plan,c)
    check_stop()
    strategy=strategy_catalog.live_selection()
    trade_request=position_analyzer.build_request(candidates,account,strategy['prompt'])
    from trading_agent import agent_model
    trade_request['model']=agent_model()
    action=plan['action']
    held_qty=int(c['paper_position']['qty'])
    quantity=plan['sell_quantity']
    from math import nextafter
    ratio=quantity/held_qty if quantity else 0
    if quantity and position_analyzer.sell_qty(held_qty,ratio)!=quantity:
        ratio=nextafter(ratio,1.0)
    d=dict(symbol=symbol,decision='SELL' if action in ('SELL','TAKE_PROFIT_ORDER') else 'HOLD',
        confidence=plan['confidence'],action_ratio=ratio,suggested_qty=quantity,
        reference_price=c['last_price'],suggested_price=c['bid'] if quantity else None,
        strategy_id='NONE',target_price=None,invalidation_price=None,holding_days=None,
        reason=plan['reason'],data_quality=c['quote_status'],warnings=[])
    if action=='TAKE_PROFIT_ORDER':
        d['suggested_price']=c['exit_context']['resting_order']['limit_price']
    decision=dict(market_view=plan['reason'],decisions=[d])
    if action=='RESEARCH_ADD':
        from agent_decision import _analyze_batch
        worker_service=SimpleNamespace(db_path=service.db_path,stop=SimpleNamespace(is_set=lambda:stopped() or service.stop.is_set()))
        decision,response=_analyze_batch(worker_service,directory,dict(run_id=run_id),trade_request,
            candidates,account,rules,True,load_rules(ROOT/'config/scan_rules.json'),broker_health=health)
        review=response.get('pretrade_review') or dict(status='NOT_NEEDED')
        validation=response.get('validation')
    else:
        position_analyzer.validate(decision,candidates,account['available_cash_cents'],
            minimum_buy_fee_twd=rules.economic_minimum_commission_twd,capital_twd=float(capital))
        review=position_analyzer.review_trades(decision,trade_request,provider or call_openai)
        validation='VALID'
    manifest=dict(run_id=run_id,status='COMPLETE',ai_validation=validation,trading_mode='agent',
        read_only=False,started_at=started,finished_at=now().isoformat(),error='',
        strategy={key:strategy[key] for key in ('name','version','sha256')})
    limits={}
    for lot in (c.get('buy_plans') or {}).get('lots',[]):
        for key in ('invalidation_price','target_price'):
            value=(lot.get('plan') or {}).get(key)
            if value is not None:
                combine=max if key=='invalidation_price' else min
                limits[key]=combine(limits.get(key,value),value)
    result=dict(manifest=manifest,batch=dict(quotes=candidates),analysis_set=candidates,decision=decision,
        pretrade_review=review,management_plan=plan,original_exit_levels=limits,
        execution_budget=dict(available_cash_twd=account['available_cash_twd'],budget_status=account['budget_status'],
            strategy_cap_twd=float(capital)),real_order_sent=False)
    path=directory/'advice_interface.json'
    write_advice_interface(result,path)
    check_stop()
    if validation=='VALID':
        if action=='CANCEL_EXIT_AND_REVIEW':
            result['execution_report']=get_broker_runtime(ROOT).cancel_exit(c['pending_exit']['decision_id'],
                lambda:stopped() or service.stop.is_set())
            plan['next_review_seconds']=policy()['minimum_review_seconds']
        elif any(d['decision'] in ('BUY','SELL') for d in decision['decisions']):
            result['execution_report']=service._execute_after_advice_written('AUTO',result,directory,path)
        else:
            result['execution_report']=dict(status='NO_TRADE_ACTION',broker_order_sent=False)
    save_json(directory/'result.json',result)
    return result
