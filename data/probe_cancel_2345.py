"""One-off, explicitly authorized probe for the October 8 2345 order."""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import redact
from execution.capital_reply_session import build_live_readonly_session_from_project
from execution.cancel_manager import cancel_verified_order
from execution.execution_store import ExecutionStore
from execution.capital_crosscheck import parse_reply_row


DECISION_ID = '20261008T112728-gui-961937a5:2345'
SEQ13 = '2313269391157'


def main():
    db_path = ROOT / 'data' / 'execution' / 'live_execution.sqlite3'
    with sqlite3.connect(f'file:{db_path.as_posix()}?mode=ro', uri=True) as db:
        row = db.execute(
            'SELECT symbol,side,started_at,status,detail_json FROM execution_attempts WHERE decision_id=?',
            (DECISION_ID,),
        ).fetchone()
    if row is None:
        raise RuntimeError('TARGET_ORDER_NOT_FOUND')
    symbol, side, started_at, status, detail_json = row
    detail = json.loads(detail_json or '{}')
    if (symbol, side, status, str(detail.get('seq13')),
            int(detail.get('quantity') or 0), int(detail.get('filled_quantity') or 0)) != (
            '2345', 'BUY', 'PARTIALLY_FILLED', SEQ13, 3, 2):
        raise RuntimeError('TARGET_ORDER_CHANGED')
    target = dict(decision_id=DECISION_ID, seq13=SEQ13, symbol='2345', side='BUY',
                  started_at=started_at, ordered_qty=3, filled_qty=2, remaining_qty=1)

    import comtypes
    comtypes.CoInitialize()
    try:
        session = build_live_readonly_session_from_project(ROOT)
        session.connect()
        session.ensure_ready()
        before = session.crosscheck_seq13(SEQ13, include_report9=False)
        evidence = dict(replay_complete=before.replay_complete,
                        acknowledged_events=len(before.accepted_rows),
                        filled_qty=before.filled_quantity,
                        cancel_events=len(before.cancel_rows))
        if not (before.replay_complete and len(before.accepted_rows) == 1
                and before.filled_quantity == 2 and not before.cancel_rows):
            print(json.dumps({'status': 'NO_CANCEL_SENT', 'evidence': evidence}))
            return
        result = cancel_verified_order(ROOT, session, target,
                                       allow_historical_test=True)
        accepted = parse_reply_row(before.accepted_rows[0])
        checks = dict(symbol_matches=accepted.symbol == '2345',
                      side_matches=accepted.buy_sell == 'B00R2',
                      side_code=accepted.buy_sell,
                      quantity_matches=accepted.quantity == 3,
                      account_matches=(accepted.parts[4].strip() + accepted.parts[5].strip()
                                       == session.account),
                      broker_field_length=len(accepted.parts[4].strip()),
                      customer_field_length=len(accepted.parts[5].strip()),
                      session_account_length=len(session.account))
        if result['status'] == 'CANCEL_REPLY_OBSERVED':
            ExecutionStore(db_path, must_exist=True).reconcile_broker_cancel(
                DECISION_ID, SEQ13, int(result['broker_filled_qty']))
        after = session.crosscheck_seq13(SEQ13, include_report9=False)
        with sqlite3.connect(f'file:{db_path.as_posix()}?mode=ro', uri=True) as db:
            ledger = db.execute('SELECT status FROM execution_attempts WHERE decision_id=?',
                                (DECISION_ID,)).fetchone()[0]
        print(json.dumps({'before': evidence, 'checks': checks,
                          'cancel_status': result['status'],
                          'skip_reason': result.get('reason'),
                          'cancel_request_sent': result['cancel_request_sent'],
                          'cancel_return_code': result.get('cancel_return_code'),
                          'cancel_message': redact(result.get('cancel_message', '')),
                          'after_cancel_events': len(after.cancel_rows),
                          'after_filled_qty': after.filled_quantity,
                          'ledger_status': ledger}, ensure_ascii=True))
    finally:
        comtypes.CoUninitialize()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status': 'PROBE_ERROR',
                          'reason': redact(f'{type(exc).__name__}: {exc}')}, ensure_ascii=True))
        raise SystemExit(1)
