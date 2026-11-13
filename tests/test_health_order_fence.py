"""No-send receipts may be retried only after another user confirmation."""
from datetime import datetime
from pathlib import Path
from threading import Event,Thread
from types import SimpleNamespace
from unittest.mock import Mock,patch
import json
import tempfile
import unittest

from execution.execution_store import ExecutionStore
from opportunity_flow.common import TAIPEI
from opportunity_flow.health_order import run_test,checks


class HealthOrderFenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.at=datetime(2026,11,13,10,tzinfo=TAIPEI)
        self.decision_id='health-0050-1-80-2026-11-13'
        self.store=ExecutionStore(self.root/'isolated.sqlite3')
        self.snapshot=dict(checked_at=self.at.isoformat(),status='OK',errors=[],broker_inventory_verified=True,inventory_checked=True,
            budget_status='READY',current_buy_budget_twd='1000',strategy_cap_twd='10000')
        quote=dict(symbol='0050',exchange='TWSE',quote_status='LIVE',is_trial=False,quote_basis='capital_intraday_odd_lot',
            quote_timestamp=self.at.isoformat(),source='ISOLATED',last_price=100,bid=99.9,ask=100)
        self.runtime=SimpleNamespace(root=self.root,read_only=False,stopped=lambda:False,can_trade_now=lambda:True,
            snapshot=self.snapshot,refresh=Mock(return_value=self.snapshot),store=self.store,
            config=SimpleNamespace(max_quote_age_seconds=120),fetch_quotes=Mock(return_value={'0050':quote}),
            broker=SimpleNamespace(execute_health_order_test=Mock()))
        self.blocked=dict(status='BLOCKED_BUDGET',block_reason='current checked allowance too small',results=[
            dict(decision_id=self.decision_id,status='SKIPPED_CHECK_FAILED',reason='current checked allowance too small',
                 broker_order_sent=False,crosscheck_performed=False)])
        at=self.at
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return at.astimezone(tz) if tz else at.astimezone().replace(tzinfo=None)
        self.clock_patch=patch('opportunity_flow.health_order.datetime',Clock)
        self.clock_patch.start();self.addCleanup(self.clock_patch.stop)
        self.fence=self.root/'data/execution/health_order_test'/(self.decision_id+'.dispatch.json')

    def run_test(self,confirmed=True):return run_test(self.runtime,{},confirmed=confirmed)[0]

    def accept(self,*_):
        item=dict(decision_id=self.decision_id,status='ACKNOWLEDGED',send_attempted=True,broker_order_sent=True,
            send_return_code=0,seq13='1234567890123',crosscheck_performed=True,crosscheck_status='ACKNOWLEDGED')
        self.assertTrue(self.store.claim(self.decision_id,self.decision_id,'0050','BUY'))
        self.store.finish(self.decision_id,'ACKNOWLEDGED',item)
        return dict(status='COMPLETE',results=[item])

    def test_preflight_no_send_keeps_reason_and_requires_new_confirmation_for_retry(self):
        self.runtime.broker.execute_health_order_test.side_effect=[self.blocked]
        first=self.run_test()
        self.assertEqual(first['status'],'BLOCKED');self.assertTrue(first['no_send_confirmed'])
        marker=json.loads(self.fence.read_text(encoding='utf-8'))
        self.assertEqual(marker['state'],'NO_SEND_CONFIRMED');self.assertEqual(len(marker['history']),1)
        self.assertEqual(marker['reason'],'current checked allowance too small')
        with self.assertRaisesRegex(ValueError,'CONFIRMATION'):self.run_test(confirmed=False)
        self.runtime.broker.execute_health_order_test.assert_called_once()
        self.runtime.broker.execute_health_order_test.side_effect=self.accept
        second=self.run_test()
        self.assertEqual(second['status'],'ACCEPTED');self.assertTrue(second['manual_retry_after_no_send'])
        self.assertEqual(second['previous_no_send_reason'],marker['reason'])
        self.assertEqual(self.runtime.broker.execute_health_order_test.call_count,2)
        self.assertEqual(len(json.loads(self.fence.read_text(encoding='utf-8'))['history']),1)
        third=self.run_test();self.assertTrue(third['duplicate'])
        self.assertEqual(self.runtime.broker.execute_health_order_test.call_count,2)

    def test_unknown_response_never_reopens_no_ledger_fence(self):
        self.runtime.broker.execute_health_order_test.side_effect=TimeoutError('reply lost')
        first=self.run_test();second=self.run_test()
        self.assertEqual(first['status'],'UNCONFIRMED');self.assertEqual(second['status'],'UNCONFIRMED')
        self.assertTrue(second['duplicate']);self.assertEqual(self.store.events_for(self.decision_id),[])
        self.runtime.broker.execute_health_order_test.assert_called_once()
        self.assertEqual(json.loads(self.fence.read_text(encoding='utf-8'))['state'],'DISPATCH_REQUESTED')

    def test_sdk_evidence_prevents_reopening_even_if_return_claims_blocked_budget(self):
        result={**self.blocked,'execution_timing':dict(events=[dict(stage='sdk_send',phase='start')])}
        self.runtime.broker.execute_health_order_test.return_value=result
        first=self.run_test();second=self.run_test()
        self.assertNotIn('no_send_confirmed',first);self.assertEqual(second['status'],'UNCONFIRMED')
        self.runtime.broker.execute_health_order_test.assert_called_once()

    def test_incomplete_no_send_receipt_cannot_authorize_retry(self):
        self.fence.parent.mkdir(parents=True)
        self.fence.write_text(json.dumps(dict(state='NO_SEND_CONFIRMED')),encoding='utf-8')
        report=self.run_test();self.assertEqual(report['status'],'UNCONFIRMED')
        self.runtime.broker.execute_health_order_test.assert_not_called()

    def test_account_lease_prevents_concurrent_diagnostic_retries(self):
        entered,release=Event(),Event();reports=[]
        def pending(*_):
            entered.set();release.wait(3)
            return self.blocked
        self.runtime.broker.execute_health_order_test.side_effect=pending
        worker=Thread(target=lambda:reports.append(self.run_test()));worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaisesRegex(RuntimeError,'其他程序'):self.run_test()
            self.runtime.broker.execute_health_order_test.assert_called_once()
        finally:release.set();worker.join(3)
        self.assertEqual(reports[0]['status'],'BLOCKED')

    def test_report_and_health_text_preserve_manual_ownership(self):
        self.runtime.broker.execute_health_order_test.side_effect=self.accept
        report=self.run_test()
        self.assertEqual(report['ownership'],'MANUAL_DIAGNOSTIC')
        self.assertEqual(report['management_policy'],'RECORD_ONLY_NO_AI_ACTION')
        findings=checks(report,self.snapshot)
        self.assertIn('AI 不接管',findings[0]['detail'])
        self.assertIn('人工',findings[-1]['remediation'])
        self.assertNotIn('增加 AI 股數',findings[-1]['remediation'])


if __name__=='__main__':unittest.main()
