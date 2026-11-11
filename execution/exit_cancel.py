"""Cancel only this system's prepared AI SELL, then wait for broker reconciliation."""
from datetime import datetime
import json
from pathlib import Path

from config import TAIPEI
from .capital_crosscheck import validate_seq13
from .execution_store import ExecutionStore


def pending_exit(store, symbol):
    with store._connect() as con:
        rows=con.execute("""SELECT decision_id,status,detail_json FROM execution_attempts
            WHERE symbol=? AND side='SELL' AND status IN ('ACKNOWLEDGED','PARTIALLY_FILLED','UNCONFIRMED')""",
            (symbol,)).fetchall()
    found=[]
    for decision_id,status,raw in rows:
        detail=json.loads(raw or '{}')
        plan=detail.get('exit_plan') or {}
        if plan.get('version')=='formal-planned-exit-v1' and detail.get('send_return_code')==0:
            found.append(dict(decision_id=decision_id,status=status,seq13=detail.get('seq13'),
                limit_price=plan.get('limit_price'),quantity=plan.get('quantity'),
                filled_quantity=int(detail.get('filled_quantity') or 0),
                cancel_requested=bool(detail.get('exit_cancel_requested_at')),
                invalidation_price=plan.get('invalidation_price')))
    return found[0] if len(found)==1 else None


def request_cancel(project_root,session,decision_id,stopped=lambda:False):
    """Run on the shared STA. rc=0 never releases shares or means cancel confirmed."""
    from .auto_advice_executor import LiveExecutionConfig
    from capital_check import unpack
    if stopped() or not LiveExecutionConfig.load(Path(project_root)/'config/execution_live.json').enabled:
        raise ValueError('EXIT_CANCEL_NOT_ACTIVE')
    if session.ensure_ready().ready is not True:
        raise ValueError('EXIT_CANCEL_BROKER_NOT_READY')
    store=ExecutionStore(Path(project_root)/'data/execution/live_execution.sqlite3',must_exist=True)
    stamp=datetime.now(TAIPEI)
    with store._connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT symbol,side,status,detail_json FROM execution_attempts WHERE decision_id=?',
                        (decision_id,)).fetchone()
        if row is None:raise ValueError('EXIT_CANCEL_ORDER_MISSING')
        symbol,side,status,raw=row
        detail=json.loads(raw or '{}');plan=detail.get('exit_plan') or {}
        if (side!='SELL' or status not in ('ACKNOWLEDGED','PARTIALLY_FILLED','UNCONFIRMED')
                or plan.get('version')!='formal-planned-exit-v1' or detail.get('send_return_code')!=0
                or not store.ai_trading_enabled(symbol)):
            raise ValueError('EXIT_CANCEL_NOT_OWN_PREPARED_SELL')
        expires=datetime.fromisoformat(plan['expires_at'])
        if expires.tzinfo is None or stamp>=expires or expires.astimezone(TAIPEI).date()!=stamp.date():
            raise ValueError('EXIT_CANCEL_EXPIRED')
        if detail.get('exit_cancel_requested_at'):
            return dict(status='CANCEL_ALREADY_REQUESTED_WAIT_FOR_BROKER',decision_id=decision_id)
        seq13=validate_seq13(str(detail.get('seq13') or ''))
        if stopped():raise ValueError('EXIT_CANCEL_STOPPED')
        # Claim before the remote call: a crash/timeout cannot cause blind retries.
        detail['exit_cancel_requested_at']=stamp.isoformat()
        con.execute('UPDATE execution_attempts SET detail_json=? WHERE decision_id=?',
                    (json.dumps(detail,ensure_ascii=False),decision_id))
        store._insert_event(con,decision_id,'EXIT_CANCEL_REQUEST_CLAIMED',stamp.isoformat(),dict(seq13=seq13))
    try:
        if stopped():raise ValueError('EXIT_CANCEL_STOPPED_BEFORE_SEND')
        code=unpack(session._skO.CancelOrderBySeqNo(session.user,False,session.account,seq13))
        result=dict(status='CANCEL_REQUESTED_WAIT_FOR_BROKER' if code==0 else 'CANCEL_REJECTED_NO_AUTOMATIC_RETRY',
                    return_code=code,decision_id=decision_id,shares_released=False,replacement_sent=False)
    except Exception as exc:
        result=dict(status='CANCEL_UNCONFIRMED_NO_AUTOMATIC_RETRY',error_type=type(exc).__name__,
                    decision_id=decision_id,shares_released=False,replacement_sent=False)
    store.append_event(decision_id,'EXIT_CANCEL_SEND_RESULT',result)
    return result
