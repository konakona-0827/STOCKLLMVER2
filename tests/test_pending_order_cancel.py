import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from config import TAIPEI
from execution.capital_crosscheck import evaluate_seq13
from execution.cancel_manager import cancel_verified_order
from execution.execution_store import ExecutionStore
from execution.health_monitor import _read_order_keys
from pending_order_ai import analyze, eligible_orders, validate


SEQ = '1234567890123'


def reply_row(kind):
    parts = [''] * 49
    parts[0], parts[1], parts[2], parts[3] = SEQ, 'TC', kind, 'N'
    parts[4], parts[5] = 'AC', 'COUNT'
    parts[6], parts[8], parts[11], parts[20], parts[47] = 'B00R2', '0050', '100', '2', SEQ
    return ','.join(parts)


def order(started_at):
    return dict(decision_id='D1', seq13=SEQ, symbol='0050', side='BUY',
                started_at=started_at, ordered_qty=2, filled_qty=0,
                remaining_qty=2, limit_price_twd='100', last_price=100,
                bid=99, ask=101, quote_status='LIVE')


class PendingOrderCancelTests(unittest.TestCase):
    def test_only_verified_same_day_order_reaches_ai(self):
        at = datetime.now(TAIPEI)
        row = dict(order(at.isoformat()), unconfirmed_qty=2,
                   broker_verified_filled_qty=0,
                   ledger_status='ACKNOWLEDGED', status='NO_FILL_YET_VERIFIED',
                   broker_event_status='ACKNOWLEDGED_NO_FILL_EVENT',
                   broker_replay_complete=True,
                   broker_event_counts={'ack': 1, 'cancel': 0})
        quote = dict(symbol='0050', quote_status='LIVE', last_price=100,
                     bid=99, ask=101, quote_timestamp=at.isoformat())
        self.assertEqual(1, len(eligible_orders({'status': 'PENDING_ORDER',
                                                'broker_order_reconciliation': [row]},
                                                [quote], at)))
        row['broker_replay_complete'] = False
        self.assertEqual([], eligible_orders({'status': 'PENDING_ORDER',
                                             'broker_order_reconciliation': [row]},
                                             [quote], at))
        row['broker_replay_complete'] = True
        row['cancel_requested'] = True
        self.assertEqual([], eligible_orders({'status': 'PENDING_ORDER',
                                             'broker_order_reconciliation': [row]},
                                             [quote], at))

    def test_model_must_answer_every_order_once(self):
        item = dict(decision_id='D1', action='CANCEL', confidence=0.9, reason='價差擴大')
        self.assertEqual('CANCEL', validate({'decisions': [item]}, [dict(decision_id='D1')])['decisions'][0]['action'])
        with self.assertRaises(ValueError):
            validate({'decisions': [item, item]}, [dict(decision_id='D1')])

    def test_ai_error_defaults_to_keep(self):
        target = order(datetime.now(TAIPEI).isoformat())
        _, decision, response = analyze([target], provider=lambda request: ('bad json', {}))
        self.assertEqual('ERROR', response['validation'])
        self.assertEqual('KEEP', decision['decisions'][0]['action'])

    def test_exact_order_cancels_once_and_waits_for_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ExecutionStore(root / 'data' / 'execution' / 'live_execution.sqlite3')
            self.assertTrue(store.claim('D1', 'R1', '0050', 'BUY'))
            detail = dict(seq13=SEQ, quantity=2, limit_price='100',
                          filled_quantity=0, send_return_code=0,
                          required_twd_with_buffer='220')
            store.mark_send_pending('D1', detail)
            store.mark_submitted('D1', detail)
            store.finish('D1', 'ACKNOWLEDGED', detail)
            rows = [reply_row('N')]
            calls = []

            def cancel(*args):
                calls.append(args)
                rows.append(reply_row('C'))
                return ('accepted', 0)

            session = SimpleNamespace(
                user='U', account='ACCOUNT', _skO=SimpleNamespace(CancelOrderBySeqNo=cancel),
                ensure_ready=lambda: None, pump=lambda seconds: None,
                crosscheck_seq13=lambda seq, **kwargs: evaluate_seq13(
                    seq, replay_rows=rows, replay_complete=True),
            )
            target = order(datetime.now(TAIPEI).isoformat())
            session.account = 'DIFFERENT'
            self.assertEqual('SKIPPED', cancel_verified_order(root, session, target)['status'])
            self.assertEqual([], calls)
            session.account = 'ACCOUNT'
            first = cancel_verified_order(root, session, target)
            self.assertEqual('CANCEL_REPLY_OBSERVED', first['status'])
            self.assertTrue(_read_order_keys(store.path)[0]['cancel_requested'])
            second = cancel_verified_order(root, session, target)
            self.assertEqual('SKIPPED', second['status'])
            self.assertEqual(1, len(calls))


if __name__ == '__main__':
    unittest.main()
