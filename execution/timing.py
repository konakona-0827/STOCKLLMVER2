"""Passive execution timing. Logging failures never retry or change an order."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps
from math import isfinite
import json
import os
from pathlib import Path
from threading import RLock
from time import perf_counter_ns
from uuid import uuid4
from decimal import Decimal,InvalidOperation

TAIPEI = timezone(timedelta(hours=8))
_trace = ContextVar('execution_trace', default=None)
_intent = ContextVar('execution_timing_intent', default={})


class ExecutionTrace:
    def __init__(self):
        self.id=uuid4().hex;self.started_at=datetime.now(TAIPEI).isoformat();self.clock=perf_counter_ns()
        self.lock=RLock();self.events=[];self.path=None;self.write_errors=[]

    def _write(self,event):
        if self.path is None:return
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with self.path.open('a',encoding='utf-8',newline='\n') as stream:
                stream.write(json.dumps(event,ensure_ascii=False,default=str,allow_nan=False)+'\n')
        except (OSError,ValueError,TypeError) as exc:
            self.write_errors.append(type(exc).__name__)

    def enable(self,root):
        with self.lock:
            if self.path is not None:return
            self.path=Path(root)/'data/execution/timing'/(self.id+'.jsonl')
            for event in self.events:self._write(event)

    def mark(self,stage,phase='instant',**details):
        with self.lock:
            event=dict(trace_id=self.id,sequence=len(self.events)+1,stage=stage,phase=phase,
                at=datetime.now(TAIPEI).isoformat(),offset_ms=round((perf_counter_ns()-self.clock)/1_000_000,3))
            event.update(_intent.get());event.update(details)
            self.events.append(event);self._write(event)
            return event

    def export(self,decision_id=None):
        with self.lock:
            events=[dict(e) for e in self.events if decision_id is None or not e.get('decision_id') or e['decision_id']==decision_id]
            return dict(version=1,trace_id=self.id,started_at=self.started_at,timezone='Asia/Taipei',process_id=os.getpid(),
                duration_clock='perf_counter_ns',elapsed_ms=round((perf_counter_ns()-self.clock)/1_000_000,3),
                record_path=str(self.path) if self.path is not None else None,write_errors=list(self.write_errors),events=events,
                metrics=metrics(events))


def current_trace():return _trace.get()
def set_trace(trace):return _trace.set(trace)
def reset_trace(token):_trace.reset(token)
def begin_intent(decision_id,symbol,side):return _intent.set(dict(decision_id=decision_id,symbol=symbol,side=side))


def mark(stage,**details):
    trace=current_trace()
    return trace.mark(stage,**details) if trace is not None else None


@contextmanager
def span(stage,**details):
    trace=current_trace()
    if trace is None:
        yield
        return
    context={k:_intent.get().get(k) for k in ('decision_id','symbol','side')}
    started=trace.mark(stage,'start',**details);clock=perf_counter_ns();error=None
    try:yield
    except BaseException as exc:error=type(exc).__name__;raise
    finally:trace.mark(stage,'end',span_start=started['sequence'],duration_ms=round((perf_counter_ns()-clock)/1_000_000,3),error=error,**{**context,**details})


def timed(stage):
    def decorate(function):
        @wraps(function)
        def call(*args,**kwargs):
            with span(stage):return function(*args,**kwargs)
        return call
    return decorate


@contextmanager
def trace_scope():
    existing=current_trace();trace=existing or ExecutionTrace();token=set_trace(trace);intent_token=_intent.set({})
    try:yield trace
    finally:_intent.reset(intent_token);reset_trace(token)


def instrument_execution(function):
    @wraps(function)
    def call(*args,**kwargs):
        with trace_scope() as trace:
            trace.enable(kwargs['project_root'])
            with span('executor_pipeline'):
                result=function(*args,**kwargs)
            for row in result.get('results',[]):
                trace.mark('execution_result',decision_id=row.get('decision_id'),symbol=row.get('symbol'),side=row.get('side'),status=row.get('status'))
            result['execution_timing']=trace.export()
            for row in result.get('results',[]):
                row['execution_timing']=trace.export(row.get('decision_id'))
            return result
    return call


def valid_record(event):
    """Reject damaged persisted fields before a reader indexes or sums them."""
    if not isinstance(event,dict):return False
    if type(event.get('sequence')) is not int or event['sequence']<=0:return False
    if not isinstance(event.get('stage'),str) or not event['stage']:return False
    if event.get('phase') not in ('start','end','instant'):return False
    try:
        if not isinstance(event.get('at'),str) or datetime.fromisoformat(event['at']).tzinfo is None:return False
    except ValueError:return False
    for key in ('decision_id','symbol','side'):
        if event.get(key) is not None and not isinstance(event[key],str):return False
    if 'span_start' in event and (type(event['span_start']) is not int or event['span_start']<=0):return False
    for key in ('duration_ms','offset_ms','queue_wait_ms'):
        if key in event and (type(event[key]) not in (int,float) or not isfinite(event[key]) or event[key]<0):return False
    return True


def metrics(events):
    durations={}
    for event in events:
        if event.get('phase')=='end' and event.get('duration_ms') is not None:
            row=durations.setdefault(event['stage'],dict(count=0,total_ms=0))
            row['count']+=1;row['total_ms']=round(row['total_ms']+event['duration_ms'],3)
    result=dict(stages=durations,stage_totals_overlap=True)
    for event in reversed(events):
        if event['stage']=='sdk_send' and event['phase']=='start':
            result['sdk_send_started_at']=event['at']
            before=[e for e in events if e.get('decision_id')==event.get('decision_id') and e['stage']=='send_boundary' and e['sequence']<event['sequence']]
            if before:
                try:
                    result['quote_age_at_sdk_send_seconds']=(datetime.fromisoformat(event['at'])-datetime.fromisoformat(before[-1]['quote_timestamp'])).total_seconds()
                except (KeyError,TypeError,ValueError):result['quote_age_at_sdk_send_seconds']=None
            proposals=[e for e in events if e['stage'] in ('buy_proposal','sell_proposal') and e.get('symbol')==event.get('symbol') and e.get('side')==event.get('side') and e['sequence']<event['sequence']]
            if proposals:
                if 'offset_ms' in event and 'offset_ms' in proposals[-1]:
                    result['proposal_to_sdk_send_ms']=round(event['offset_ms']-proposals[-1]['offset_ms'],3)
                reviewed=proposals[-1].get('research_reviewed_at')
                if reviewed:
                    try:result['research_to_sdk_send_seconds']=(datetime.fromisoformat(event['at'])-datetime.fromisoformat(reviewed)).total_seconds()
                    except (ValueError,TypeError):pass
            break
    result['broker_queue_wait_ms']=round(sum(e.get('queue_wait_ms',0) for e in events if e['stage']=='broker_worker_started'),3)
    return result


def observe_fill(result,quote,side):
    """Use confirmed broker trade rows only, never the order's limit price."""
    from .capital_crosscheck import parse_reply_row
    rows=[]
    side_value=str(getattr(side,'value',side));seq=getattr(result,'seq13',None)
    for raw in getattr(result.crosscheck,'fill_rows',()) or ():
        try:
            row=parse_reply_row(raw)
            if (row and row.is_fill and row.symbol==getattr(result,'symbol',None)
                    and row.buy_sell==('B' if side_value=='BUY' else 'S') and seq and row.contains_seq(str(seq))
                    and row.price is not None and row.price.is_finite() and row.price>0 and row.quantity and row.quantity>0):rows.append(row)
        except (ValueError,TypeError,ArithmeticError):pass
    quantity=sum(r.quantity for r in rows)
    if not result.filled_quantity or quantity!=result.filled_quantity:
        mark('fill_price_observation',verified=False,realized_slippage_bps=None,reason='BROKER_PRICED_FILLS_NOT_VERIFIED')
        return
    try:
        price=sum(r.price*r.quantity for r in rows)/quantity
        reference=Decimal(str(quote.ask if str(getattr(side,'value',side))=='BUY' else quote.bid))
        if not reference.is_finite() or reference<=0:raise InvalidOperation
        adverse=(price-reference if str(getattr(side,'value',side))=='BUY' else reference-price)/reference*10000
        mark('fill_price_observation',verified=True,filled_quantity=quantity,fill_vwap=str(price),
             reference_price=str(reference),reference_basis='LAST_OBSERVED_EXECUTABLE_QUOTE_NOT_A_NEW_SEND_TIME_QUOTE',
             realized_slippage_bps=str(adverse),source='BROKER_TRADE_ROWS')
    except (ValueError,TypeError,ArithmeticError):
        mark('fill_price_observation',verified=False,realized_slippage_bps=None,reason='EXECUTABLE_REFERENCE_NOT_VERIFIED')
