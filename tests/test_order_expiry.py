import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from execution.execution_store import ExecutionStore
from execution.health_monitor import reconcile_confirmed_broker_events


class OrderExpiryTests(unittest.TestCase):
    def test_prior_day_partial_fill_closes_remainder_without_cancel_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'data' / 'execution' / 'live_execution.sqlite3'
            store = ExecutionStore(path)
            store.claim('D1', 'R1', '2345', 'BUY')
            detail = dict(seq13='1234567890123', quantity=3, filled_quantity=2,
                          limit_price='100', send_return_code=0,
                          required_twd_with_buffer='320')
            store.mark_send_pending('D1', detail)
            store.mark_submitted('D1', detail)
            store.finish('D1', 'PARTIALLY_FILLED', detail)
            prior_day = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
            with sqlite3.connect(path) as db:
                db.execute('UPDATE execution_attempts SET started_at=? WHERE decision_id=?',
                           (prior_day, 'D1'))
            report = dict(errors=[], inventory_checked=True,
                          inventory_comparison=[dict(symbol='2345', broker_cash_qty=2,
                                                     configured_user_qty=0)],
                          broker_order_reconciliation=[dict(
                              decision_id='D1', symbol='2345', side='BUY',
                              seq13='1234567890123', broker_replay_complete=True,
                              broker_replay_filled_qty=2, ledger_filled_qty=2,
                              broker_verified_filled_qty=2, ordered_qty=3,
                              broker_event_counts=dict(ack=1, cancel=0),
                              broker_event_status='EXPIRED_BY_EXCHANGE_RULE',
                              status='FILL_MATCH')])
            outcome = reconcile_confirmed_broker_events(root, report)
            self.assertEqual([], outcome['errors'])
            self.assertEqual('PARTIALLY_FILLED_EXPIRED', outcome['updated'][0]['status'])
            with sqlite3.connect(path) as db:
                status, saved = db.execute(
                    'SELECT status,detail_json FROM execution_attempts WHERE decision_id=?',
                    ('D1',)).fetchone()
            self.assertEqual('PARTIALLY_FILLED_EXPIRED', status)
            self.assertEqual(1, json.loads(saved)['exchange_session_expired_qty'])
            self.assertEqual([], reconcile_confirmed_broker_events(root, report)['errors'])

    def test_same_day_order_cannot_be_marked_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(Path(tmp) / 'x.sqlite3')
            store.claim('D1', 'R1', '2345', 'BUY')
            detail = dict(seq13='1234567890123', quantity=3, filled_quantity=2,
                          send_return_code=0, required_twd_with_buffer='320')
            store.mark_send_pending('D1', detail)
            store.mark_submitted('D1', detail)
            store.finish('D1', 'PARTIALLY_FILLED', detail)
            with self.assertRaises(RuntimeError):
                store.reconcile_session_expiry('D1', '1234567890123', 2)


if __name__ == '__main__':
    unittest.main()
