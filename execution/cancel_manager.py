"""Single-attempt cancellation of an exact, verified AI-managed TC order."""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from config import TAIPEI
from .capital_crosscheck import parse_reply_row, validate_seq13
from .capital_executor import parse_send_result
from .execution_store import ExecutionStore


def cancel_verified_order(project_root: str | Path, session, order: dict,
                          *, allow_historical_test: bool = False) -> dict:
    decision_id = str(order['decision_id'])
    seq13 = validate_seq13(str(order['seq13']))
    remaining = int(order['remaining_qty'])
    result = dict(decision_id=decision_id, seq13=seq13, symbol=order['symbol'],
                  side=order['side'], requested_remaining_qty=remaining,
                  status='SKIPPED', cancel_request_sent=False, automatic_retry=False)
    if (remaining < 1 or (not allow_historical_test and
            datetime.fromisoformat(order['started_at']).astimezone(TAIPEI).date()
            != datetime.now(TAIPEI).date())):
        result['reason'] = 'ORDER_NOT_ACTIVE_TODAY'
        return result

    session.ensure_ready()
    session.pump(0.25)
    cross = session.crosscheck_seq13(seq13, include_report9=False)
    accepted = [parse_reply_row(raw) for raw in cross.accepted_rows]
    account_matches = (len(accepted) == 1 and len(accepted[0].parts) > 5
                       and accepted[0].parts[4].strip() + accepted[0].parts[5].strip()
                       == session.account)
    if (not cross.replay_complete or len(accepted) != 1 or cross.cancel_rows
            or not account_matches
            or cross.filled_quantity >= int(order['ordered_qty'])
            or cross.filled_quantity != int(order['filled_qty'])
            or not accepted[0].is_normal_order
            or accepted[0].symbol != order['symbol']
            or accepted[0].buy_sell != ('B00R2' if order['side'] == 'BUY' else 'S00R2')
            or accepted[0].quantity != int(order['ordered_qty'])):
        result['reason'] = 'BROKER_ORDER_CHANGED_OR_UNVERIFIED'
        return result
    store = ExecutionStore(Path(project_root) / 'data' / 'execution' / 'live_execution.sqlite3',
                           must_exist=True)
    if not store.claim_cancel(decision_id, seq13, remaining):
        result['reason'] = 'ALREADY_ATTEMPTED_OR_LEDGER_CHANGED'
        return result

    # A crash or timeout after this point leaves the no-retry marker in place.
    try:
        raw = session._skO.CancelOrderBySeqNo(session.user, False, session.account, seq13)
        sent = parse_send_result(raw)
        result.update(cancel_request_sent=(sent.return_code == 0),
                      cancel_return_code=sent.return_code,
                      cancel_message=sent.message)
        store.append_event(decision_id, 'CANCEL_API_RETURN',
                           {'seq13': seq13, 'return_code': sent.return_code,
                            'message': sent.message})
        if sent.return_code != 0:
            result['status'] = 'BROKER_REJECTED_OR_UNVERIFIED'
            return result
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            session.pump(min(0.5, deadline - time.monotonic()))
            after = session.crosscheck_seq13(seq13, include_report9=False)
            if after.cancel_rows:
                result['status'] = 'CANCEL_REPLY_OBSERVED'
                result['broker_filled_qty'] = after.filled_quantity
                return result
        result['status'] = 'CANCEL_REPLY_PENDING'
        return result
    except Exception as exc:
        result.update(status='CANCEL_OUTCOME_UNVERIFIED',
                      reason=f'{type(exc).__name__}: {exc}')
        return result
