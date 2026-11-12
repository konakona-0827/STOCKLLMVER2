"""Cancellation receipts do not release shares, fabricate fills or cause retries."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from execution.execution_store import ExecutionStore
from execution.exit_cancel import pending_exit,request_cancel
from execution.models import Side
from test_profit_exit_integration import Clock
from test_profit_exit_position_manager import AT,holding,management
from position_manager import validate_plan


class PositionExitCancelTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);(self.root/'config').mkdir()
        (self.root/'config/execution_live.json').write_text(json.dumps(dict(enabled=True)),encoding='utf-8')
        self.store=ExecutionStore(self.root/'data/execution/live_execution.sqlite3')
        self.store.bootstrap_ai_qty('1815',30)
        self.session=SimpleNamespace(user='test-user',account='test-account',ensure_ready=Mock(return_value=SimpleNamespace(ready=True)),
            _skO=SimpleNamespace(CancelOrderBySeqNo=Mock(return_value=('request accepted',0))))
        self.detail=dict(seq13='1234567890123',send_return_code=0,quantity=30,filled_quantity=0,
            pricing_policy='PLANNED_LIMIT',exit_plan=dict(version='formal-planned-exit-v1',limit_price=140,
                quantity=30,expires_at=AT.replace(hour=13,minute=30).isoformat()))

    def order(self,*,side='SELL',version='formal-planned-exit-v1',filled=0):
        detail=deepcopy(self.detail);detail['exit_plan']['version']=version;detail['filled_quantity']=filled
        self.store.claim('D','R','1815',side)
        self.store.mark_send_pending('D',detail)
        self.store.mark_submitted('D',detail)
        if filled:
            self.store.finish_with_fill('D','PARTIALLY_FILLED',detail,'1815',Side.SELL,filled)
        else:
            self.store.finish('D','ACKNOWLEDGED',detail)

    def cancel(self,stopped=lambda:False):
        with patch('execution.exit_cancel.datetime',Clock):
            return request_cancel(self.root,self.session,'D',stopped)

    def test_cancel_request_waits_for_actual_broker_confirmation(self):
        self.order()
        result=self.cancel()
        self.assertEqual(result['status'],'CANCEL_REQUESTED_WAIT_FOR_BROKER')
        self.assertFalse(result['shares_released'])
        self.assertFalse(result['replacement_sent'])
        self.assertEqual(self.store.pending_reconciliation_symbols(),{'1815'})
        self.assertEqual(self.store.get_ai_qty('1815'),30)
        self.session._skO.CancelOrderBySeqNo.assert_called_once_with('test-user',False,'test-account','1234567890123')
        self.assertTrue(pending_exit(self.store,'1815')['cancel_requested'])
        self.assertEqual(self.cancel()['status'],'CANCEL_ALREADY_REQUESTED_WAIT_FOR_BROKER')
        self.session._skO.CancelOrderBySeqNo.assert_called_once()
        self.store.reconcile_broker_cancel('D','1234567890123',0)
        self.assertEqual(self.store.pending_reconciliation_symbols(),set())
        self.assertIsNone(pending_exit(self.store,'1815'))
        self.assertEqual(self.store.get_ai_qty('1815'),30)

    def test_partial_fill_keeps_exact_remaining_shares_during_cancel(self):
        self.order(filled=10)
        self.assertEqual(self.store.get_ai_qty('1815'),20)
        self.assertEqual(pending_exit(self.store,'1815')['filled_quantity'],10)
        self.cancel()
        self.assertEqual(self.store.get_ai_qty('1815'),20)
        with self.assertRaises(RuntimeError):
            self.store.reconcile_broker_cancel('D','1234567890123',0)
        self.store.reconcile_broker_cancel('D','1234567890123',10)
        self.assertEqual(self.store.get_ai_qty('1815'),20)

    def test_rejected_or_uncertain_cancel_is_never_blindly_retried(self):
        for failure in (False,True):
            with self.subTest(exception=failure):
                # Reset only the isolated fixture between independent scenarios.
                if failure:
                    with self.store._connect() as con:
                        con.execute('DELETE FROM execution_attempts')
                        con.execute('DELETE FROM execution_events')
                self.order()
                self.session._skO.CancelOrderBySeqNo.reset_mock()
                if failure:self.session._skO.CancelOrderBySeqNo.side_effect=RuntimeError('ambiguous network')
                else:self.session._skO.CancelOrderBySeqNo.return_value=('rejected',1002)
                result=self.cancel()
                self.assertIn('NO_AUTOMATIC_RETRY',result['status'])
                self.cancel()
                self.session._skO.CancelOrderBySeqNo.assert_called_once()
                self.assertEqual(self.store.pending_reconciliation_symbols(),{'1815'})

    def test_other_order_types_and_user_stop_cannot_cancel(self):
        self.order(version='ordinary-manual-exit')
        with self.assertRaisesRegex(ValueError,'NOT_OWN_PREPARED_SELL'):self.cancel()
        self.session._skO.CancelOrderBySeqNo.assert_not_called()
        with self.assertRaisesRegex(ValueError,'NOT_ACTIVE'):self.cancel(lambda:True)

    def test_pending_exit_allows_cancel_review_but_never_second_sell(self):
        c=holding();c['pending_execution']=True;c['pending_exit']=dict(decision_id='D')
        plan=management('CANCEL_EXIT_AND_REVIEW')
        self.assertEqual(validate_plan(plan,c)['action'],'CANCEL_EXIT_AND_REVIEW')
        with self.assertRaisesRegex(ValueError,'PENDING_EXIT_MUST_BE_CONFIRMED'):
            validate_plan(management('SELL',30),c)
        c['pending_exit']=None
        with self.assertRaisesRegex(ValueError,'NO_OWN_PENDING_EXIT'):
            validate_plan(plan,c)


if __name__=='__main__':unittest.main()
