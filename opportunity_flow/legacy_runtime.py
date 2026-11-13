"""Lazy adapter to the existing account, COM owner, risk checks and execution ledger."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
import os
import threading
import time
import sqlite3
from pathlib import Path

from .common import TAIPEI,digest,save,stamp
from .activity import tracked


class LegacyRuntime:
    def __init__(self,state_root,output,model,*,read_only=False,stop_event=None):
        from runtime_paths import validate_state_root
        selected=validate_state_root(state_root)
        os.environ['TRADING_STATE_ROOT']=str(selected)
        from config import ROOT,load_env
        if ROOT.resolve()!=selected:
            raise RuntimeError('ACCOUNT_STATE_ALREADY_IMPORTED_FOR_ANOTHER_ROOT')
        load_env()
        from execution.auto_advice_executor import LiveExecutionConfig
        from execution.execution_store import ExecutionStore
        from execution.broker_runtime import get_broker_runtime
        from main import Audit
        self.root,self.output,self.model=selected,Path(output),model
        self.read_only,self.stop_event=read_only,stop_event
        self.operation_lock=threading.RLock()
        if not read_only:
            from .fees import fee_status,correct_legacy_capital_fee
            if fee_status(selected)['basis']=='LEGACY_RESERVE_USED_AS_COMMISSION':
                correct_legacy_capital_fee(selected,self.output/'fee_review')
        self.config=LiveExecutionConfig.load(selected/'config/execution_live.json')
        if not read_only and not self.config.enabled:
            raise RuntimeError('EXISTING_EXECUTOR_IS_DISABLED')
        self.store=ExecutionStore(selected/'data/execution/live_execution.sqlite3',must_exist=True)
        self.output.mkdir(parents=True,exist_ok=True)
        self.audit=Audit(self.output)
        self.broker=get_broker_runtime(selected)
        self.account_identity=digest(str(selected))
        self.snapshot=None
        self.observations={};self.disclosures={}
        from config import CODE_ROOT
        self.support_policy=json.loads((CODE_ROOT/'config/support_agents.json').read_text(encoding='utf-8-sig'))['policy']

    def stopped(self):
        return self.stop_event is not None and self.stop_event.is_set()

    def can_trade_now(self):
        from scheduler import is_twse_oddlot_market_window
        return is_twse_oddlot_market_window(datetime.now(TAIPEI).timestamp())

    @tracked('BROKER_ACCOUNT', '券商對帳／核對帳本')
    def refresh(self):
        from execution.health_monitor import sync_broker_state
        from execution.auto_advice_executor import LiveExecutionConfig
        self.config=LiveExecutionConfig.load(self.root/'config/execution_live.json')
        self.health,self.snapshot=sync_broker_state(self.root,runtime=self.broker,return_report=True)
        return self.snapshot

    def market_map(self,symbols,dataset):
        from universe import load_universe
        legacy=load_universe(self.root/'config/universe_tw.json')
        etfs=set(legacy.get('etf_symbols',()))
        markets={}
        for s in symbols:
            exchange=(dataset.get('master',{}).get(s) or {}).get('exchange')
            if not exchange and dataset['grouped'].get(s):exchange=dataset['grouped'][s][-1]['exchange']
            if exchange in ('TWSE','TPEX'):markets[s]=5 if exchange=='TWSE' else 6
            elif s in etfs:markets[s]=5  # Existing configured TWSE ETF universe.
        return markets

    @tracked('BROKER_QUOTES', '取得券商零股行情')
    def fetch_quotes(self,symbols,dataset):
        from capital_multi_quote import multi_snapshot
        markets=self.market_map(symbols,dataset)
        started=datetime.now(TAIPEI).isoformat()
        raw,errors=self.broker.fetch_quotes_with_markets(symbols,markets,self.audit,self.stopped,timeout=12)
        for s in list(raw):
            if raw[s].get('venue')!=('TWSE' if markets.get(s)==5 else 'TPEx'):
                raw.pop(s);errors[s]='BROKER_RETURNED_VENUE_MISMATCH'
        result=multi_snapshot(symbols,raw,errors,started,datetime.now(TAIPEI).isoformat())
        for q in result['quotes']:
            q['exchange']='TWSE' if markets.get(q['symbol'])==5 else 'TPEX' if markets.get(q['symbol'])==6 else None
            q['source_market_number']=markets.get(q['symbol'])
            if q['quote_status']=='LIVE' and q.get('is_trial') is False:
                history=self.observations.setdefault(q['symbol'],[])
                if not history or (stamp(q['quote_timestamp'])-stamp(history[-1]['exchange_time'])).total_seconds()>=60:
                    history.append(dict(symbol=q['symbol'],exchange_time=q['quote_timestamp'],price=q['last_price'],
                        quote_status='LIVE',is_trial=False,retrieval_mode='BROKER'))
                    self.observations[q['symbol']]=history[-60:]
        self.quotes_received_at=datetime.now(TAIPEI).isoformat()
        return {q['symbol']:q for q in result['quotes']}

    def candidates(self,symbols,quotes,dataset):
        from analysis_service import apply_live_position_context
        positions=self.store.ai_positions()
        rows={r['symbol']:r for r in (self.health or {}).get('inventory_comparison',[])}
        candidates=[]
        for s in symbols:
            q=deepcopy(quotes.get(s) or {})
            held=int(positions.get(s,0));r=rows.get(s,{})
            c={**q,'symbol':s,'name':q.get('name') or (dataset.get('master',{}).get(s) or {}).get('name') or s,
                'asset_type':'ETF' if s.startswith('00') else 'STOCK',
                'quote_status':q.get('quote_status','UNAVAILABLE'),'last_price':q.get('last_price'),
                'position':dict(ai_managed_qty=held,user_qty=int(r.get('configured_user_qty') or 0)),
                'paper_position':dict(qty=held,cost_cents=None,source='FORMAL_CONFIRMED_AI_LEDGER'),
                'pricing_policy':'AT_QUOTE','eligible_strategies':[], 'max_buy_qty':0,
                'analysis_source':'TW_OPPORTUNITY_FLOW', 'market_history':deepcopy(self.observations.get(s,[])), 'price_levels':[],
                'company_disclosures':deepcopy(self.disclosures.get(s,{'status':'SOURCE_UNAVAILABLE'})),
                'daily_bars':[{**b,'complete':True,'source':b['exchange']+'_DAILY'} for b in dataset['grouped'].get(s,[]) if b['date']<datetime.now(TAIPEI).date().isoformat()][-60:]}
            c['daily_history_status']=dict(bars=len(c['daily_bars']),latest_date=c['daily_bars'][-1]['date'] if c['daily_bars'] else None,
                price_basis='RAW_UNADJUSTED_UNVERIFIED_ACTIONS')
            candidates.append(c)
        apply_live_position_context(candidates,self.health,self.store.path,self.audit)
        pending=self.store.pending_reconciliation_symbols()
        from execution.exit_cancel import pending_exit
        from profit_exit import refresh
        for c in candidates:
            c['pending_execution']=c['symbol'] in pending
            c['pending_exit']=pending_exit(self.store,c['symbol']) if c['pending_execution'] else None
            c['pricing_policy']='PLANNED_LIMIT' if self.config.planned_limits_enabled else 'AT_QUOTE'
            c['user_handoff']=self.handoff_context(c['symbol'],c['paper_position']['qty'])
            h=c['user_handoff']
            if h and h.get('remaining_manual_unit_cost_twd') is not None:
                c['ai_cost_basis']=dict(available=True,average_unit_cost_twd=h['remaining_manual_unit_cost_twd'],quantity=c['paper_position']['qty'],
                    estimated=False,excludes_fees_and_tax=True,source=h['cost_source'])
            refresh(c,self.config)
        return candidates

    def handoff_context(self,symbol,held):
        if held<=0:return None
        with self.store._connect() as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='manual_ai_handoffs'").fetchone():return None
            con.row_factory=sqlite3.Row
            row=con.execute('SELECT * FROM manual_ai_handoffs WHERE symbol=? ORDER BY id DESC LIMIT 1',(symbol,)).fetchone()
            handoffs=[dict(r) for r in con.execute('SELECT * FROM manual_ai_handoffs WHERE symbol=? ORDER BY id',(symbol,))]
            attempts=[dict(r) for r in con.execute('SELECT side,started_at,detail_json FROM execution_attempts WHERE symbol=?',(symbol,))]
        if row is None:return None
        from .handoff_basis import remaining_manual_basis
        try:unit=remaining_manual_basis(handoffs,attempts,held)
        except (ValueError,KeyError,TypeError,ArithmeticError):unit=None
        return {**dict(row),'source':'USER_REQUESTED_HANDOFF','original_buy_plan_known':False,'remaining_manual_unit_cost_twd':unit,
            'note':'使用者轉交管理，交接時間不是買進日期；原買進計畫未知，勿補造失效理由。轉交股數是歷史紀錄，目前持股/可賣量另以帳本及券商核對。'}

    def transfer(self,symbol,quantity,unit_cost=None):
        from execution.position_configuration import save_ai_holding_configuration
        if self.read_only or self.stopped():raise ValueError('只讀或停止狀態不可變更持股歸屬')
        if type(quantity) is not int or quantity<=0:raise ValueError('轉交股數須為正整數')
        with self.operation_lock:
            snapshot=self.refresh()
            rows={r['symbol']:r for r in snapshot.get('inventory_comparison',[])}
            row=rows.get(symbol)
            if not row:raise ValueError('券商沒有此現股持倉')
            previous=int(snapshot.get('ai_positions',{}).get(symbol,0))
            broker_cost=(snapshot.get('verified_position_costs') or {}).get(symbol) or {}
            usable=(broker_cost.get('verified') is True and broker_cost.get('quantity')==row['broker_cash_qty'] and previous==0)
            if usable:
                unit=broker_cost.get('average_unit_cost_twd');source='VERIFIED_BROKER_COST'
            else:
                if unit_cost is None:raise ValueError('券商介面未取得可核實的轉入成本，請填寫每股成本價')
                unit=unit_cost;source='USER_INPUT'
            result=save_ai_holding_configuration(self.root,self.root/'data/analysis/market_history.sqlite3',symbol,
                previous+quantity,snapshot,unit_cost_twd=unit,handoff_source=source)
            result.update(transferred_quantity=quantity,cost_source=source,original_buy_plan_known=False,order_sent=False)
            self.refresh()
            return result

    def contexts(self,dataset,quotes):
        from pretrade_policy import buy_block
        from opportunity_engine import existing_position_risk
        from .engine import levels
        snapshot=self.snapshot or {}
        held=self.store.ai_positions();pending=self.store.pending_reconciliation_symbols()
        settings=self.store.position_settings()
        positions={c['symbol']:c for c in self.candidates([s for s,q in held.items() if q>0],quotes,dataset)}
        context={}
        for symbol,bars in dataset['grouped'].items():
            c=positions.get(symbol);basis=(c or {}).get('ai_cost_basis') or {}
            risks={};reason=None
            if c:
                reason=buy_block(c)
                if not basis.get('available'):reason='EXISTING_AI_COST_UNKNOWN'
                if c.get('ask') and c.get('bid') and basis.get('available'):
                    for stop in levels(bars[-60:])[0] if bars else []:
                        try:risks[str(float(stop['price']))]=float(existing_position_risk(c,stop['price'],self.config))
                        except (ValueError,ArithmeticError):reason='EXISTING_POSITION_RISK_UNKNOWN'
            context[symbol]=dict(source='LIVE_ACCOUNT_CONTEXT',account_identity=self.account_identity,
                available_cash_twd=float(snapshot.get('current_buy_budget_twd') or 0),
                held_qty=int(held.get(symbol,0)),average_cost_twd=basis.get('average_unit_cost_twd'),risk_by_stop=risks,
                trading_enabled=settings.get(symbol,{}).get('enabled',True),pending_execution=symbol in pending,
                buy_block_reason=reason,minimum_fee_reserve_twd=float(self.config.min_buy_fee_reserve_twd),
                buy_cash_buffer_rate=float(self.config.buy_cash_buffer_rate),checked_at=snapshot.get('checked_at'))
        return context

    def configure_position(self,symbol,quantity,unit_cost=None):
        """Total managed shares, matching the existing Dashboard ownership editor."""
        from execution.position_configuration import save_ai_holding_configuration
        if self.read_only or self.stopped():raise ValueError('只讀或停止狀態不可變更持股歸屬')
        if type(quantity) is not int or quantity<0:raise ValueError('AI 管理股數須為非負整數')
        with self.operation_lock:
            snapshot=self.refresh()
            previous=int(snapshot.get('ai_positions',{}).get(symbol,0))
            source=None;unit=None
            if quantity>previous:
                row=next((r for r in snapshot.get('inventory_comparison',[]) if r['symbol']==symbol),{})
                broker=(snapshot.get('verified_position_costs') or {}).get(symbol) or {}
                if broker.get('verified') is True and broker.get('quantity')==row.get('broker_cash_qty') and previous==0:
                    unit=broker.get('average_unit_cost_twd');source='VERIFIED_BROKER_COST'
                else:
                    unit=unit_cost;source='USER_INPUT'
                    if unit is None:raise ValueError('增加 AI 管理股數時，請輸入轉入股數的每股成本')
            result=save_ai_holding_configuration(self.root,self.root/'data/analysis/market_history.sqlite3',symbol,
                quantity,snapshot,unit_cost_twd=unit,handoff_source=source)
            result.update(cost_source=source,original_buy_plan_known=False,order_sent=False)
            self.refresh();return result

    def effective_policy(self,p):
        from .common import policy
        p=deepcopy(p)
        cap=(self.snapshot or {}).get('strategy_cap_twd')
        if cap is None or Decimal(str(cap))<=0:raise ValueError('FORMAL_CAPITAL_LIMIT_UNKNOWN')
        p['capital_twd']=float(cap)
        p['minimum_commission_twd']=float(self.config.economic_minimum_commission_twd)
        return p

    def account(self):
        s=self.snapshot or {}
        return dict(source='CONFIGURED_CAP_REMAINING_WITH_BROKER_FUNDS_AND_RESERVE_CHECKS',
            capital_limit_twd=float(s.get('strategy_cap_twd') or 0),
            available_cash_twd=float(s.get('current_buy_budget_twd') or 0),
            available_cash_cents=int(Decimal(str(s.get('current_buy_budget_twd') or 0))*100),
            budget_status=s.get('budget_status','UNVERIFIED'))

    def provider(self,request):
        if self.stopped():raise RuntimeError('USER_STOPPED')
        from codex_cli import CodexClient
        from .state import Ledger
        request={k:v for k,v in request.items() if k not in ('usage_path',)}
        request['model']=self.model
        began=time.monotonic();ledger=Ledger(self.output.parent)
        role='pretrade' if request.get('text',{}).get('format',{}).get('name')=='pretrade_review' else 'position_exit'
        try:
            response=CodexClient(timeout=180).responses.create(**request)
            usage=response.usage.model_dump()
            ledger.call(datetime.now(TAIPEI).isoformat(),role,'VALID',dict(usage=usage,elapsed_seconds=time.monotonic()-began,model=self.model))
            return response.output_text,dict(runtime='codex-cli',billing_source='chatgpt-plan',api_fallback=False,usage=usage)
        except Exception as exc:
            ledger.call(datetime.now(TAIPEI).isoformat(),role,'ERROR',dict(usage=None,error=type(exc).__name__))
            raise
        finally:ledger.close()

    @tracked('POSITION_REVIEW', '持股研究與退出檢查')
    def review_exits(self,candidates):
        import position_analyzer
        from position_manager import plan_request,validate_plan
        decisions=[];plans=[];cancels=[];errors=[]
        from math import nextafter
        for c in candidates:
            if self.stopped():break
            try:
                raw,api=self.provider(plan_request(c,self.account()))
                plan=validate_plan(json.loads(raw),c)
            except Exception as exc:
                error=type(exc).__name__+':'+str(exc)[:180]
                errors.append(dict(symbol=c['symbol'],error=error))
                plan=dict(symbol=c['symbol'],action='HOLD',sell_quantity=0,confidence=0,
                    reason='模型或格式錯誤，未形成有效持倉判斷：'+error,next_review_seconds=60,
                    watch_above=None,watch_below=None,required_data=['LIVE_ODDLOT'])
            plans.append(plan)
            action=plan['action'];qty=plan['sell_quantity'];held=c['paper_position']['qty']
            ratio=qty/held if qty else 0
            if qty and position_analyzer.sell_qty(held,ratio)!=qty:ratio=nextafter(ratio,1.0)
            if action=='CANCEL_EXIT_AND_REVIEW':
                cancels.append(dict(symbol=c['symbol'],decision_id=c['pending_exit']['decision_id'],reason=plan['reason']))
            c['flow_exit_action']=action
            decisions.append(dict(symbol=c['symbol'],decision='SELL' if action in ('SELL','TAKE_PROFIT_ORDER') else 'HOLD',
                confidence=plan['confidence'],action_ratio=ratio,suggested_qty=qty,reference_price=c['last_price'],
                suggested_price=(c['exit_context']['resting_order']['limit_price'] if action=='TAKE_PROFIT_ORDER' else c['bid']) if qty else None,
                strategy_id='NONE',target_price=None,invalidation_price=None,holding_days=None,
                reason=plan['reason'],data_quality=c['quote_status'],warnings=[]))
        result=dict(market_view='沿用最新持倉管理與費後獲利退出判斷',decisions=decisions)
        position_analyzer.validate(result,candidates,self.account()['available_cash_cents'],
            minimum_buy_fee_twd=self.config.economic_minimum_commission_twd,capital_twd=self.account()['capital_limit_twd'])
        return result,dict(validation='VALID',management_plans=plans,cancellations=cancels,errors=errors)

    def cancel_exit(self,decision_id):
        if self.read_only or self.stopped():return dict(status='READ_ONLY_OR_STOPPED',broker_order_sent=False)
        return self.broker.cancel_exit(decision_id,self.stopped)

    @tracked('PRETRADE', '下單前模型複核')
    def pretrade(self,decisions,candidates):
        import position_analyzer
        request=dict(model=self.model,input=json.dumps(dict(analysis_set=candidates,
            paper_account=self.account(),paper_simulation_rules=dict(minimum_commission_twd=float(self.config.economic_minimum_commission_twd),commission_rate=.001425)),ensure_ascii=False))
        result=dict(market_view='TW Opportunity Flow 執行前複核',decisions=decisions)
        review=position_analyzer.review_trades(result,request,self.provider)
        return result['decisions'],review

    @tracked('EXECUTOR_HANDOFF', '原執行器／券商送單核對')
    def execute(self,payload,path):
        if self.stopped():return dict(status='USER_STOPPED',results=[],broker_order_sent=False)
        if self.read_only:return dict(status='READ_ONLY',results=[],broker_order_sent=False)
        from execution.timing import current_trace
        if current_trace() is not None:current_trace().enable(self.root)
        from config import redact
        save(path,json.loads(redact(json.dumps(payload,ensure_ascii=False))))
        return self.broker.execute_if_active(path,self.stopped)

    def close(self):
        from execution.broker_runtime import shutdown_broker_runtime
        return shutdown_broker_runtime(self.root)
