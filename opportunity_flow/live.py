"""Foreground research plus a separate, serialized live execution worker."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import importlib.util
from pathlib import Path
import threading
import time

from .common import ROOT,TAIPEI,read,save,stamp


def resolve_account_root(explicit=None):
    from runtime_paths import resolve_state_root,validate_state_root
    return validate_state_root(explicit) if explicit else resolve_state_root(ROOT)


def doctor(explicit=None):
    root=resolve_account_root(explicit)
    cfg=read(root/'config/execution_live.json')
    deps={name:importlib.util.find_spec(name) is not None for name in ('openai','agents','jsonschema','comtypes','tkinter')}
    return dict(account_state_root=str(root),existing_account_reused=True,dependencies=deps,
        executor_enabled=cfg.get('enabled') is True,planned_limits_enabled=cfg.get('planned_limits_enabled') is True,
        commission_assumption_twd=cfg.get('minimum_commission_twd',cfg.get('min_buy_fee_reserve_twd',20)),
        broker_login_tested=False,orders_sent=0,ready=all(deps.values()) and cfg.get('enabled') is True)


class LiveCoordinator:
    def __init__(self,account_root,state,p,stop_event=None,*,read_only=False,runtime=None):
        from .legacy_runtime import LegacyRuntime
        from .execution_bridge import ExecutionBridge
        self.state=Path(state);self.state.mkdir(parents=True,exist_ok=True)
        self.policy=p;self.stop=stop_event or threading.Event()
        self.runtime=runtime or LegacyRuntime(account_root,self.state/'broker',p['model'],read_only=read_only,stop_event=self.stop)
        self.bridge=ExecutionBridge(self.runtime,self.state,p)
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='flow-execution')
        self.future=None;self.pending=None;self.last_refresh=0;self.last_quote_refresh=0;self.quote_cursor=0;self.quotes={};self.events={};self.contexts={};self.snapshot={}
        self.lock=threading.RLock();self.closed=False;self.dataset=None
        self.last_maintenance=0

    def publish(self,**extra):
        with self.lock:
            rows=[]
            for row in self.snapshot.get('inventory_comparison',[]):
                symbol=row['symbol'];basis=(self.snapshot.get('ai_committed_positions') or [])
                cost=next((b for b in basis if b['symbol']==symbol),{})
                broker_cost=(self.snapshot.get('verified_position_costs') or {}).get(symbol) or {}
                rows.append(dict(symbol=symbol,broker_qty=row.get('broker_cash_qty'),
                    manual_qty=max(0,int(row.get('broker_cash_qty') or 0)-int(row.get('ai_qty') or 0)),
                    ai_qty=row.get('ai_qty'),broker_cost=broker_cost.get('average_unit_cost_twd') if broker_cost.get('verified') else None,
                    ai_cost=cost.get('average_unit_cost_twd'),user_handoff=self.runtime.handoff_context(symbol,int(row.get('ai_qty') or 0))))
            status=dict(system='TW Opportunity Flow Live',checked_at=self.snapshot.get('checked_at'),
                account_identity=self.runtime.account_identity,budget_status=self.snapshot.get('budget_status'),
                buy_budget_twd=self.snapshot.get('current_buy_budget_twd'),positions=rows,
                read_only=self.runtime.read_only,stopping=self.stop.is_set(),execution_running=self.future is not None,
                broker_quote_count=len(self.quotes),errors=self.snapshot.get('errors',[]),**extra)
            save(self.state/'live_status.json',status)
            return status

    def refresh_account(self):
        with self.lock:
            self.snapshot=self.runtime.refresh();self.last_refresh=time.monotonic()
            self.publish()
            return self.snapshot

    def pulse(self,dataset,previous=None):
        from .company_events import current_events,for_symbol
        self.dataset=dataset
        if time.monotonic()-self.last_refresh>=60 or not self.snapshot:self.refresh_account()
        if time.monotonic()-self.last_quote_refresh>=60:
            held=[s for s,q in self.runtime.store.ai_positions().items() if q>0]
            research=sorted([c for c in (previous or {}).get('candidates',[]) if c.get('packet')],
                key=lambda c:(c['state'] not in ('RESEARCH_READY','REVIEW_PENDING','REVIEW_IN_FLIGHT'),-c.get('rank_score',0),c['symbol']))
            if not research:
                from .engine import build
                p=self.runtime.effective_policy(self.policy)
                research=[build(s,dataset,datetime.now(TAIPEI),p) for s in dataset['grouped']]
                research=sorted([c for c in research if c['state'] in ('REVIEW_PENDING','ECONOMIC_BLOCKED')],
                    key=lambda c:(c['state']!='REVIEW_PENDING',-c.get('rank_score',0),c['symbol']))
            urgent=[c['symbol'] for c in research[:12]]
            exploration=[c['symbol'] for c in research[12:]]
            rotated=[]
            if exploration:
                self.quote_cursor%=len(exploration)
                size=min(20-len(urgent),len(exploration))
                rotated=(exploration+exploration)[self.quote_cursor:self.quote_cursor+size]
                self.quote_cursor=(self.quote_cursor+size)%len(exploration)
            symbols=list(dict.fromkeys(held+urgent+rotated))
            self.quotes=self.runtime.fetch_quotes(symbols,dataset) if symbols else {}
            source=current_events(self.state/'company_events',datetime.now(TAIPEI))
            self.events={}
            for s in symbols:
                if not dataset['grouped'].get(s):continue
                try:self.events[s]=for_symbol(source,s,(dataset.get('master',{}).get(s) or {}).get('exchange') or
                    dataset['grouped'][s][-1]['exchange'],datetime.now(TAIPEI),self.policy['holding_sessions'])
                except (ValueError,KeyError,TypeError):
                    self.events[s]=dict(status='SOURCE_UNAVAILABLE',blockers=['COMPANY_EVENT_PARSE_ERROR'])
            self.last_quote_refresh=time.monotonic()
        self.runtime.disclosures=deepcopy(self.events)
        # Reconcile context after any fills/handoff; quote errors never use MIS as execution evidence.
        self.contexts=self.runtime.contexts(dataset,self.quotes)
        self.publish()
        return self.runtime.effective_policy(self.policy),deepcopy(self.quotes),deepcopy(self.events),deepcopy(self.contexts)

    def offer(self,result,dataset):
        self.drain()
        if self.stop.is_set():return
        ready=any(c['state']=='RESEARCH_READY' for c in result['candidates'])
        if self.future is not None:
            if ready:self.pending=(deepcopy(result),dataset)
        elif ready or any(q>0 for q in self.runtime.store.ai_positions().values()):
            self.pending=None
            self.future=self.pool.submit(self.bridge.step,deepcopy(result),dataset,datetime.now(TAIPEI))

    def drain(self):
        if self.future is not None and self.future.done():
            try:
                report=self.future.result();save(self.state/'latest_flow_execution.json',report)
                self.publish(last_execution=report)
            except Exception as exc:
                error=dict(status='EXECUTION_CYCLE_ERROR',error=type(exc).__name__+':'+str(exc)[:180])
                save(self.state/'latest_flow_execution.json',error);self.publish(last_execution=error)
            self.future=None
        if self.future is None and self.pending is not None and not self.stop.is_set():
            result,dataset=self.pending;self.pending=None
            self.future=self.pool.submit(self.bridge.step,result,dataset,datetime.now(TAIPEI))

    def maintenance(self,force=False):
        """Existing holdings keep their exit path even if daily collection fails."""
        self.drain()
        if (force and self.runtime.can_trade_now() and self.dataset is not None
                and time.monotonic()-self.last_maintenance>=60):
            self.last_maintenance=time.monotonic()
            self.offer(dict(candidates=[]),self.dataset)

    def transfer(self,symbol,quantity,cost=None):
        with self.lock:
            result=self.runtime.transfer(symbol,quantity,cost)
            self.snapshot=deepcopy(self.runtime.snapshot);self.contexts={};self.last_refresh=0
            self.publish(last_handoff=result)
            return result

    def close(self):
        if self.closed:return
        self.stop.set();self.pool.shutdown(wait=True,cancel_futures=True)
        with self.lock:self.runtime.close();self.closed=True
