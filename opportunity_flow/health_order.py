"""One fixed diagnostic intent after the user accepts the UI confirmation."""
from datetime import datetime
import json
import os

from .common import TAIPEI,read,save,stamp
from .data import validate_quote


def build_payload(quote,snapshot,at):
    from execution.health_order_test import POLICY,SOURCE,trading_date
    at=stamp(at);decision_id='health-0050-1-80-'+at.date().isoformat()
    outside=quote is None
    quote=quote or dict(source='USER_CONFIRMED_FIXED_LIMIT_NOT_MARKET_DATA',quote_timestamp=at.isoformat(),
        last_price=None,bid=None,ask=None)
    quote_status='USER_FIXED_LIMIT' if outside else 'LIVE'
    return dict(interface_version='1.0',generated_at=at.isoformat(),run_id=decision_id,run_status='READY',
        llm_validation='VALID',llm_validation_scope='SCHEMA_ONLY_USER_ASSUMPTION',llm_model='NO_MODEL_DIAGNOSTIC',
        real_order_sent=False,diagnostic_assumption=True,actual_model_calls=0,
        ownership='MANUAL_DIAGNOSTIC',management_policy='RECORD_ONLY_NO_AI_ACTION',
        valid_trading_date=trading_date(at),requested_outside_session=outside,
        health_order_test=dict(symbol='0050',side='BUY',quantity=1,limit_price='80',time_in_force='ROD',user_confirmed=True),
        execution_budget=dict(available_cash_twd='100',budget_status='MANUAL_CASH_RECHECK_REQUIRED',
            strategy_cap_twd=None,budget_scope='MANUAL_DIAGNOSTIC_ACCOUNT_CASH'),
        market=dict(quotes=[dict(symbol='0050',quote_status=quote_status,source=quote['source'],price=quote['last_price'],
            bid=quote['bid'],ask=quote['ask'],exchange_time=quote['quote_timestamp'])]),
        market_view='使用者接受的 0050 一股 80 元 ROD 診斷假設；非投資研究結論。',
        recommendations=[dict(decision_id=decision_id,symbol='0050',decision='BUY',confidence=0,
            suggested_price=80,suggested_qty=1,reference_price=quote['last_price'],action_ratio=0,ai_managed_qty=0,
            reason='使用者已在視窗確認：0050 買進 1 股、最高 80 元、ROD；檢查送單與核對，不追價或重送。',
            analysis_source=SOURCE,pricing_policy=POLICY,quote_status=quote_status,quote_timestamp=quote['quote_timestamp'],
            quote_age_seconds=(at-stamp(quote['quote_timestamp'])).total_seconds(),warnings=['DIAGNOSTIC_ASSUMPTION_NOT_RESEARCH'])])


def outcome(item):
    accepted=(item.get('broker_order_sent') is True and item.get('send_return_code')==0 and bool(item.get('seq13'))
        and item.get('crosscheck_performed') is True and item.get('crosscheck_status') in
        ('ACKNOWLEDGED','PARTIALLY_FILLED','FILLED','CANCELLED'))
    if accepted:return 'ACCEPTED'
    if item.get('send_return_code') not in (None,0) or item.get('status')=='REJECTED':return 'REJECTED'
    if item.get('send_attempted') or item.get('status') in ('STARTED','SEND_PENDING','SUBMITTED','UNCONFIRMED'):return 'UNCONFIRMED'
    return 'BLOCKED'


def run_test(runtime,dataset,*,confirmed=False,progress=None):
    if confirmed is not True:raise ValueError('USER_CONFIRMATION_REQUIRED')
    if runtime.read_only:raise ValueError('READ_ONLY_HEALTH_ORDER_TEST_DISABLED')
    from execution.timing import trace_scope,span
    from state_lock import StateLease
    progress=progress or (lambda label:None)
    with StateLease(runtime.root,'health-order-test'),trace_scope() as trace:
        trace.enable(runtime.root)
        with span('health_order_test'):
            report=_run(runtime,dataset,progress)
        report['execution_timing']=trace.export()
        save(runtime.root/'data/execution/health_order_test/latest.json',report)
    return report,runtime.snapshot or {}


def _confirmed_no_send(execution,store,decision_id):
    """Only the original executor's early no-send returns can reopen a fence."""
    if execution.get('status') not in ('BLOCKED_BUDGET','DISABLED','USER_STOPPED'):return False
    if store.events_for(decision_id):return False
    if any(row.get('send_attempted') or row.get('broker_order_sent') is not False
           for row in execution.get('results') or []):return False
    events=(execution.get('execution_timing') or {}).get('events') or []
    return not any(event.get('stage')=='sdk_send' and event.get('phase')=='start' for event in events)


def _run(runtime,dataset,progress):
    at=datetime.now(TAIPEI);decision_id='health-0050-1-80-'+at.date().isoformat()
    report=dict(checked_at=at.isoformat(),decision_id=decision_id,symbol='0050',side='BUY',quantity=1,limit_price='80',
        ownership='MANUAL_DIAGNOSTIC',management_policy='RECORD_ONLY_NO_AI_ACTION',
        time_in_force='ROD',user_confirmed=True,status='BLOCKED',actual_model_calls=0,diagnostic_assumption=True,
        decision_generated=False,executor_called=False,new_send_attempted=False,automatic_retry=False,
        repricing=False,automatic_cancel=False)
    try:
        if runtime.stopped():raise ValueError('USER_STOPPED')
        progress('診斷測試：查券商、庫存及原買進額度')
        snapshot=runtime.refresh()
        previous=runtime.store.events_for(decision_id)
        if previous:
            item=dict(previous[-1]['detail']);item['status']=previous[-1]['status']
            report.update(status=outcome(item),duplicate=True,decision_generated=True,previous_execution=item,
                reason='本日已有此診斷紀錄；只重新核對，不重送。')
            return report
        dispatch_path=runtime.root/'data/execution/health_order_test'/(decision_id+'.dispatch.json')
        dispatch=None
        if dispatch_path.exists():
            try:dispatch=read(dispatch_path)
            except (OSError,ValueError,TypeError):dispatch=None
            if (not isinstance(dispatch,dict) or dispatch.get('state')!='NO_SEND_CONFIRMED'
                    or dispatch.get('decision_id')!=decision_id
                    or dispatch.get('execution_status') not in ('BLOCKED_BUDGET','DISABLED','USER_STOPPED')
                    or not isinstance(dispatch.get('history'),list) or not dispatch['history']):
                report.update(status='UNCONFIRMED',duplicate=True,
                    reason='已有診斷交接紀錄，但正式委託尚未核實；保留原紀錄，不重送。')
                return report
            report.update(manual_retry_after_no_send=True,previous_no_send_reason=dispatch.get('reason'))
        from .execution_bridge import verified_snapshot
        verified_snapshot(snapshot,datetime.now(TAIPEI),need_buying_power=False)
        if snapshot.get('errors') or snapshot.get('status')=='ERROR':raise ValueError('BROKER_ACCOUNT_UNVERIFIED')
        if snapshot.get('broker_inventory_verified') is not True:raise ValueError('BROKER_INVENTORY_UNVERIFIED')
        if not runtime.store.ai_trading_enabled('0050'):raise ValueError('AI_TRADING_DISABLED_FOR_0050')
        if '0050' in runtime.store.pending_reconciliation_symbols():raise ValueError('0050_HAS_UNRESOLVED_ORDER')
        quote=None
        if runtime.can_trade_now():
            progress('診斷測試：取得真實 0050 零股行情')
            raw=runtime.fetch_quotes(['0050'],dataset).get('0050')
            quote,error=validate_quote(raw,'0050','TWSE',datetime.now(TAIPEI),float(runtime.config.max_quote_age_seconds))
            if error:raise ValueError(error)
        else:progress('診斷測試：盤外固定價請求，由券商判定預約收單；不用假行情')
        if runtime.stopped():raise ValueError('USER_STOPPED_BEFORE_DIAGNOSTIC_DISPATCH')
        payload=build_payload(quote,snapshot,datetime.now(TAIPEI));report.update(decision_generated=True,
            requested_outside_session=payload['requested_outside_session'],valid_trading_date=payload['valid_trading_date'])
        path=runtime.root/'data/execution/health_order_test'/(decision_id+'.advice.json')
        save(path,payload)
        marker=dict(decision_id=decision_id,at=datetime.now(TAIPEI).isoformat(),state='DISPATCH_REQUESTED',
            history=list(dispatch.get('history') or []) if dispatch else [])
        try:
            if dispatch:
                # The account-scoped lease excludes concurrent retries. Keep all
                # prior no-send receipts, then fence this new manual acceptance.
                save(dispatch_path,marker)
            else:
                with dispatch_path.open('x',encoding='utf-8') as stream:
                    json.dump(marker,stream)
                    stream.flush();os.fsync(stream.fileno())
        except FileExistsError:
            report.update(status='UNCONFIRMED',duplicate=True,reason='此診斷交接已由另一工作取得；不重送。')
            return report
        progress('診斷測試：交給原執行器，保留 80 元限價與原資金風控')
        report['executor_called']=True
        execution=runtime.broker.execute_health_order_test(path,runtime.stopped)
        report['execution']=execution
        rows=execution.get('results') or [];item=rows[0] if rows else {}
        report.update(status=outcome(item),new_send_attempted=bool(item.get('send_attempted')),
            reason=item.get('reason') or execution.get('block_reason') or item.get('status') or execution.get('status'))
        if _confirmed_no_send(execution,runtime.store,decision_id):
            receipt=dict(at=datetime.now(TAIPEI).isoformat(),state='NO_SEND_CONFIRMED',
                execution_status=execution['status'],reason=report['reason'])
            marker.update(receipt,history=marker['history']+[receipt])
            try:
                save(dispatch_path,marker)
                report['no_send_confirmed']=True
            except OSError as exc:
                # A failed receipt never removes the original conservative fence.
                report['dispatch_record_error']=type(exc).__name__
    except Exception as exc:
        from config import redact
        report.update(status='UNCONFIRMED' if report['executor_called'] else 'BLOCKED',
            reason=redact(type(exc).__name__+': '+str(exc)))
        # An exception after dispatch does not prove that no order was sent.
        if report['executor_called']:report['new_send_attempted']=None
    finally:
        if report['executor_called']:
            progress('診斷測試：重新查券商並核對正式帳本')
            try:runtime.refresh()
            except Exception as exc:report['reconciliation_error']=type(exc).__name__
    return report


def checks(report,snapshot):
    """Read existing evidence only; never submit, retry, cancel or invent a fill."""
    rows=[]
    def add(key,label,status,detail,remediation):
        rows.append(dict(key=key,label=label,status=status,detail=detail,remediation=remediation))
    add('diagnostic_decision','測試假設決策','PASS' if report.get('decision_generated') else 'WARN',
        '已產生 0050／BUY／1股／80元／ROD 人工診斷假設；模型呼叫 0 次，AI 不接管此委託。' if report.get('decision_generated') else str(report.get('reason') or '尚未產生診斷委託'),
        '此項檢查資料交接；真實選股研究仍由原 FLOW 執行。')
    status=report.get('status');item=(report.get('execution',{}).get('results') or [report.get('previous_execution') or {}])[0]
    seq=item.get('seq13')
    matched=[r for r in snapshot.get('broker_order_reconciliation',[]) if r.get('decision_id')==report.get('decision_id') and seq and r.get('seq13')==seq]
    check=matched[-1] if matched else {}
    from execution.health_monitor import VERIFIED_BROKER_ORDER_STATUSES
    reconciled=check.get('status') in VERIFIED_BROKER_ORDER_STATUSES
    if status=='UNCONFIRMED' and reconciled and check['status']!='ROD_EXPIRED':status='ACCEPTED'
    add('diagnostic_submission','測試單券商受理','PASS' if status=='ACCEPTED' else 'FAIL' if status=='REJECTED' else 'WARN',
        (('券商收單回報有證據；預計交易日 '+str(item.get('valid_trading_date') or report.get('valid_trading_date'))+'；尚不表示已成交。') if status=='ACCEPTED' else
         '券商拒絕，送單通道有回應，但未通過受理檢查。' if status=='REJECTED' else
         '委託結果尚未確認；不能視為沒有送單。' if status=='UNCONFIRMED' else '送單前被阻擋：'+str(report.get('reason'))),
        '人工診斷只記錄核對；AI 不追價、撤單或重送。已核實未送單且無委託帳本時，可由本人再次接受重試。')
    add('diagnostic_reconciliation','測試單與正式帳本對帳','PASS' if reconciled and not report.get('reconciliation_error') else 'WARN',
        str(check.get('status') or report.get('reconciliation_error') or '尚無可核實的這筆券商委託紀錄'),
        '保留人工診斷與券商核對紀錄；成交歸人工持股，AI 不接管或處理留單。')
    return rows
