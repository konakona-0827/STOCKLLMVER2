"""Adapt FLOW research to the existing Dashboard without running legacy scans."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading
import time

from .common import policy,read


STATE_LABELS={
    'DATA_BLOCKED':'資料待補','ACCOUNT_BLOCKED':'帳戶條件阻擋','SCREENED_OUT':'流動性未達','NO_SETUP':'無目前型態',
    'ECONOMIC_BLOCKED':'費後／風險未過','REVIEW_PENDING':'等待研究','REVIEW_IN_FLIGHT':'研究進行中',
    'REVIEW_ERROR':'研究錯誤','REVIEW_ERROR_BACKOFF':'研究錯誤退避','REVIEW_SERVICE_BACKOFF':'模型服務退避',
    'REVIEW_DEFERRED_SERVICE_ERROR':'模型服務失敗延後','RESEARCH_READY':'雙方研究通過',
    'RESEARCH_WATCH':'研究觀察','RESEARCH_REJECT':'研究否決',
}


class FlowController:
    def __init__(self,account_root,args):
        self.account_root=Path(account_root);self.args=args;self.state=Path(args.state)
        self.stop_event=threading.Event();self.wake=threading.Event()
        self.view_changed=threading.Event()
        self.thread=None;self.coordinator=None;self.status='STOPPED';self.error='';self.lock=threading.RLock()

    @property
    def running(self):return self.thread is not None and self.thread.is_alive()

    def start(self):
        with self.lock:
            if self.running:return False
            self.stop_event=threading.Event();self.wake=threading.Event();self.error='';self.status='STARTING'
            self.view_changed.set()
            self.thread=threading.Thread(target=self._run,name='opportunity-flow-watch',daemon=False)
            self.thread.start();return True

    def _run(self):
        try:
            from .live import LiveCoordinator
            from .__main__ import main
            p=policy()
            if self.args.model:p['model']=self.args.model
            if self.stop_event.is_set():return
            self.coordinator=LiveCoordinator(self.account_root,self.state,p,self.stop_event,read_only=self.args.read_only,on_change=self.view_changed.set)
            if self.stop_event.is_set():return
            self.coordinator.refresh_account();self.status='RUNNING'
            argv=['watch','--dataset',str(self.args.dataset),'--state',str(self.state),'--live','--llm','--public-quotes','--refresh-daily']
            if self.args.model:argv+=['--model',self.args.model]
            if self.args.read_only:argv+=['--read-only']
            main(argv,coordinator=self.coordinator,external_stop=self.stop_event,external_wake=self.wake)
        except Exception as exc:
            from config import redact
            self.error=redact(type(exc).__name__+': '+str(exc));self.status='ERROR'
            self.view_changed.set()
        finally:
            self.stop_event.set()
            if self.coordinator is not None:
                try:self.coordinator.close()
                except Exception as exc:
                    self.error='CLOSE_ERROR:'+type(exc).__name__;self.status='ERROR'
            if self.status!='ERROR':self.status='STOPPED'
            self.view_changed.set()

    def stop(self):
        self.stop_event.set();self.wake.set()
        if self.running:self.status='STOPPING'
        self.view_changed.set()

    def request_update(self):
        if self.running and not self.stop_event.is_set():self.wake.set();return True
        return self.start()

    def runtime_action(self,operation,*,write=False):
        if write and self.args.read_only:raise ValueError('只讀模式不可變更持股歸屬')
        with self.lock:
            c=self.coordinator
            if self.running:
                if c is None or c.closed or self.status!='RUNNING' or self.stop_event.is_set():raise RuntimeError('FLOW 正在啟動或停止，請稍後重試')
                with c.lock:return operation(c.runtime,c)
            from .legacy_runtime import LegacyRuntime
            runtime=LegacyRuntime(self.account_root,self.state/'manual_refresh',self.args.model or policy()['model'],
                read_only=not write,stop_event=threading.Event())
            try:return operation(runtime,None)
            finally:runtime.close()

    def observation(self):
        """Do not wait on the controller/broker lock from the Tk main thread."""
        c=self.coordinator
        tracker=getattr(c,'activity',None) if c is not None else None
        return dict(status=self.status,error=self.error,running=self.running,
            activity=tracker.snapshot() if tracker is not None else dict(active=[],recent=[]),
            quotes_received_at=getattr(getattr(c,'runtime',None),'quotes_received_at',None),
            quote_count=len(c.quotes) if c is not None and hasattr(c,'quotes') else 0,
            live_quote_count=sum(q.get('quote_status')=='LIVE' for q in c.quotes.values()) if c is not None and hasattr(c,'quotes') else 0)

    def refresh_account(self,progress=None):
        progress=progress or (lambda label:None)
        def work(runtime,coordinator):
            progress('正在查詢券商並核對正式帳本')
            snapshot=runtime.refresh()
            from .data import load_dataset
            try:dataset=coordinator.dataset if coordinator and coordinator.dataset is not None else load_dataset(self.args.dataset)
            except (OSError,ValueError):dataset=None
            if dataset is not None and runtime.can_trade_now():
                symbols=[r['symbol'] for r in snapshot.get('inventory_comparison',[]) if int(r.get('broker_cash_qty') or 0)>0]
                if symbols:
                    progress(f'正在取得 {len(symbols)} 檔持股的零股行情')
                    quotes=runtime.fetch_quotes(symbols,dataset)
                    snapshot={**snapshot,'position_quotes':list(quotes.values())}
            elif not runtime.can_trade_now():
                progress('目前非交易時段，保留最後行情時間')
                snapshot={**snapshot,'quote_check_status':'OUTSIDE_TRADING_SESSION'}
            self.last_diagnostic_quotes_at=getattr(runtime,'quotes_received_at',None)
            if coordinator:
                coordinator.snapshot=deepcopy(snapshot);coordinator.last_refresh=time.monotonic();coordinator.publish()
            return snapshot
        return self.runtime_action(work)

    def configure_position(self,symbol,quantity,cost):
        def work(runtime,coordinator):
            result=runtime.configure_position(symbol,quantity,cost)
            if coordinator:
                coordinator.snapshot=deepcopy(runtime.snapshot);coordinator.last_refresh=0;coordinator.publish()
            return result
        return self.runtime_action(work,write=True)


def project_research(flow,names=None):
    """Display-only old Dashboard shape; never an advice/execution payload."""
    names=names or {};summary=flow.get('summary',{})
    candidates=flow.get('candidates',[])
    ranked=sorted([c for c in candidates if c.get('packet')],key=lambda c:(c['state']!='RESEARCH_READY',-c.get('rank_score',0),c['symbol']))
    quotes=[];analysis=[];top=[]
    for index,c in enumerate(ranked,1):
        s=c['symbol'];q={**(c.get('quote') or {}),'symbol':s,'name':names.get(s,s)}
        q.setdefault('quote_status','LIVE' if c.get('quote') else 'UNAVAILABLE');q.setdefault('last_price',None);q.setdefault('quote_timestamp',None)
        quotes.append(q)
        analysis.append(dict(symbol=s,name=names.get(s,s),analysis_source='TW_OPPORTUNITY_FLOW',quant_rank=index,
            quant_score=c.get('rank_score'),flow_state=c['state'],position=dict(ai_managed_qty=0),warnings=c.get('reasons',[])))
        top.append(dict(symbol=s,rank=index,quant_score=c.get('rank_score'),quote_status=q['quote_status']))
    at=summary.get('as_of')
    return dict(display_only=True,flow=flow,
        manifest=dict(run_id='flow-view-'+str(at),finished_at=at,started_at=at,status='COMPLETE',error='',
            llm_response_received_at=at,universe_count=summary.get('symbols_observed',0),run_directory='',
            strategy=dict(name='TW Opportunity Flow',version='1.1',path='opportunity_flow/policy.json')),
        batch=dict(quotes=quotes,symbols_received=sum(q['quote_status'] in ('LIVE','PUBLIC_REFERENCE') for q in quotes)),
        top=top,analysis_set=analysis,
        decision=dict(market_view='研究狀態與送單紀錄分開顯示；研究通過仍待正式複核。',decisions=[]))


class FlowViewService:
    def __init__(self,account_root,args,controller=None):
        self.account_root=Path(account_root);self.directory=self.account_root/'data/analysis';self.db_path=self.directory/'market_history.sqlite3'
        self.args=args;self.controller=controller or FlowController(account_root,args);self.position_manager_active=False
        self.flow={};self.execution={};self.usage={};self.activity={};self.names={};self.read_errors=[];self._cache={};self.lock=threading.RLock()
        self._view_signature=None;self._view_result=None
        self._last_verified_health=None

    @property
    def stop(self):return self.controller.stop_event

    def _read(self,path,default):
        path=Path(path)
        if not path.exists():return default
        signature=(path.stat().st_mtime_ns,path.stat().st_size)
        saved=self._cache.get(str(path))
        if saved and saved[0]==signature:return saved[1]
        value=read(path);self._cache[str(path)]=(signature,value);return value

    def refresh_broker_state(self,refresh_quotes=True):return self.controller.refresh_account()

    def _display_health(self,health):
        if health and health.get('broker_inventory_verified') is True and not health.get('errors'):
            self._last_verified_health=deepcopy(health)
            return health
        if not health:return health
        saved=self._last_verified_health
        if saved is None:
            path=self.account_root/'data/execution/live_execution.sqlite3'
            if path.exists():
                try:
                    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
                        for (raw,) in db.execute('SELECT payload_json FROM broker_refresh_snapshots ORDER BY id DESC LIMIT 50'):
                            candidate=json.loads(raw)
                            if candidate.get('broker_inventory_verified') is True and not candidate.get('errors'):
                                saved=candidate;break
                except (sqlite3.Error,ValueError):pass
        if saved and not health.get('inventory_comparison'):
            # Display-only fallback. Never restore buying power or mark old
            # inventory as current evidence for the execution path.
            health=deepcopy(health)
            health['inventory_comparison']=[dict(row,status='LAST_VERIFIED_NOT_CURRENT') for row in saved.get('inventory_comparison',[])]
            health['inventory_display_source']='LAST_VERIFIED_BROKER_SNAPSHOT'
            health['inventory_display_checked_at']=saved.get('checked_at')
            health['broker_inventory_verified']=False
        return health

    def _signature(self):
        paths=[Path(self.args.state)/name for name in ('latest.json','latest_flow_execution.json','public_quotes.json','live_status.json')]
        paths += [Path(self.args.dataset)/'master.json',self.account_root/'config/execution_live.json']
        for db in (self.db_path,self.account_root/'data/execution/live_execution.sqlite3',Path(self.args.state)/'flow.sqlite3'):
            paths += [db,Path(str(db)+'-wal')]
        result=[]
        for path in paths:
            try:stat=path.stat();result.append((str(path),stat.st_mtime_ns,stat.st_size))
            except FileNotFoundError:result.append((str(path),None,None))
        return tuple(result)

    def check_health(self,progress=None):
        from .health import inspect_health
        from .common import save
        try:snapshot=self.controller.refresh_account(progress=progress)
        except Exception as exc:
            from config import redact
            snapshot=dict(status='ERROR',errors=[redact(type(exc).__name__+': '+str(exc))])
        report=inspect_health(self.account_root,self.args.state,self.args.dataset,snapshot,
            controller_status=self.controller.status,controller_error=self.controller.error,read_only=self.args.read_only,
            progress=progress)
        save(Path(self.args.state)/'health/latest.json',report)
        return report

    def refresh(self,sync_broker=False):
        from execution.health_monitor import latest_refresh_snapshot
        import position_store
        with self.lock:
            signature=self._signature()
            if not sync_broker and signature==self._view_signature and self._view_result is not None:
                return self._view_result
            c=self.controller.coordinator
            health=self.refresh_broker_state() if sync_broker else deepcopy(c.snapshot) if c and c.snapshot else latest_refresh_snapshot(self.account_root)
            health=self._display_health(health)
            self.read_errors=[]
            try:
                flow=self._read(Path(self.args.state)/'latest.json',{})
                execution=self._read(Path(self.args.state)/'latest_flow_execution.json',{})
                if (not isinstance(flow,dict) or not isinstance(flow.get('summary',{}),dict)
                        or not isinstance(flow.get('candidates',[]),list) or not isinstance(execution,dict)
                        or any(not isinstance(c,dict) or not isinstance(c.get('symbol'),str)
                            or not isinstance(c.get('state'),str)
                            or (c.get('packet') is not None and not isinstance(c['packet'],dict))
                            or (c.get('review') is not None and not isinstance(c['review'],dict))
                            for c in flow.get('candidates',[]))):
                    raise ValueError('FLOW_VIEW_SCHEMA_INVALID')
                self.flow,self.execution=flow,execution
            except (OSError,ValueError,TypeError) as exc:self.read_errors.append('FLOW_READ_ERROR:'+type(exc).__name__)
            try:
                master=self._read(Path(self.args.dataset)/'master.json',{})
                if not isinstance(master,dict) or any(not isinstance(v,dict) for v in master.values()):raise ValueError('MASTER_VIEW_INVALID')
            except (OSError,ValueError):master={}
            names={s:m.get('name',s) for s,m in master.items()};self.names=names
            result=project_research(self.flow,names);result['manifest']['run_directory']=str(Path(self.args.state).resolve())
            try:
                public_payload=self._read(Path(self.args.state)/'public_quotes.json',{})
                if not isinstance(public_payload,dict):raise ValueError('PUBLIC_VIEW_INVALID')
                public=public_payload.get('quotes',[])
                if not isinstance(public,list) or any(not isinstance(q,dict) or not q.get('symbol') for q in public):raise ValueError('PUBLIC_VIEW_INVALID')
            except (OSError,ValueError,TypeError):public=[];self.read_errors.append('PUBLIC_VIEW_READ_ERROR')
            observed={q['symbol']:q for q in public}
            observed.update({q['symbol']:q for q in (health or {}).get('position_quotes',[])})
            if c is not None:observed.update(deepcopy(c.quotes))
            merged={q['symbol']:q for q in result['batch']['quotes']}
            for s,q in observed.items():
                if s not in merged or q.get('quote_status')=='LIVE' or merged[s]['quote_status']!='LIVE':
                    merged[s]={**q,'name':q.get('name') or names.get(s,s)}
            result['batch']['quotes']=list(merged.values())
            result['batch']['symbols_received']=sum(q.get('quote_status') in ('LIVE','PUBLIC_REFERENCE') for q in merged.values())
            for row in result['top']:row['quote_status']=merged[row['symbol']]['quote_status']
            positions=[]
            if self.db_path.exists():
                with closing(sqlite3.connect(self.db_path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
                    db.row_factory=sqlite3.Row
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='positions'").fetchone():
                        positions=[dict(row) for row in db.execute('SELECT * FROM positions ORDER BY symbol')]
            latest={q['symbol']:q for q in (health or {}).get('position_quotes',[])}
            positions=[position_store.context(p,latest.get(p['symbol'],{}).get('last_price')) for p in positions]
            self.usage=self._usage()
            from .common import digest
            result['_flow_view_revision']=digest(signature)
            self._view_signature=signature;self._view_result=(result,positions,health)
            return self._view_result

    def _usage(self):
        path=Path(self.args.state)/'flow.sqlite3';totals={};calls=0;unknown=0
        if not path.exists():return dict(calls=0,usage={},unknown_calls=0)
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as con:
                for (raw,) in con.execute('SELECT data FROM calls'):
                    item=json.loads(raw);calls+=1;usage=item.get('usage')
                    if usage is None:unknown+=1
                    else:
                        for key,value in usage.items():
                            if isinstance(value,(int,float)):totals[key]=totals.get(key,0)+value
                self.activity=dict(calls=[dict(at=at,role=role,status=status,error=json.loads(raw).get('error'))
                    for at,role,status,raw in con.execute('SELECT at,role,status,data FROM calls ORDER BY id DESC LIMIT 15')],
                    feeds=[dict(at=at,kind=kind,data=json.loads(raw))
                    for at,kind,raw in con.execute('SELECT at,kind,data FROM feed_events ORDER BY id DESC LIMIT 10')])
            return dict(calls=calls,usage=totals,unknown_calls=unknown)
        except (sqlite3.Error,ValueError):return dict(calls=None,usage={},unknown_calls=None,error='USAGE_READ_ERROR')

    def run(self,*args,**kwargs):raise RuntimeError('FLOW_UI_MUST_NOT_RUN_LEGACY_SCAN')
