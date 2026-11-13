"""Diagnostic UI/decision/original executor tests; every SDK call is isolated."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import tempfile
import unittest

from opportunity_flow.common import TAIPEI,save
from opportunity_flow.health_order import build_payload,run_test,checks
from execution.execution_store import ExecutionStore
from execution.account_guard import BuyingPower
from execution.account_snapshot import ExecutionAccountSnapshot
from execution.auto_advice_executor import execute_advice_file
from execution.capital_crosscheck import CrossCheckResult
from execution.models import ExecutionStatus


class HealthOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.at=datetime(2026,11,13,10,0,tzinfo=TAIPEI)
        self.snapshot=dict(checked_at=self.at.isoformat(),status='OK',errors=[],inventory_checked=True,
            broker_inventory_verified=True,budget_status='READY',current_buy_budget_twd='1000',strategy_cap_twd='10000')
        self.quote=dict(symbol='0050',exchange='TWSE',quote_status='LIVE',is_trial=False,quote_basis='capital_intraday_odd_lot',
            quote_timestamp=self.at.isoformat(),source='CAPITAL_SKCOM',last_price=100,bid=99.9,ask=100)
        self.store=ExecutionStore(self.root/'isolated.sqlite3')
        self.cfg=self.root/'config.json';save(self.cfg,dict(enabled=True,min_available_to_buy_twd=5000,
            min_buy_fee_reserve_twd=20,minimum_commission_twd=1,max_daily_buy_twd=30000,observe_seconds=0))
        self.balance=BuyingPower(True,True,Decimal(30000),Decimal(30000),Decimal(30000),'ISOLATED')
        self.sender=Mock(return_value=('1234567890123',0))
        self.session=SimpleNamespace(connected=True,user='ISOLATED',account='ISOLATED',
            _sk=SimpleNamespace(STOCKORDER=lambda:SimpleNamespace(nTradeType=None,nSpecialTradeType=None)),_skO=SimpleNamespace(SendStockOddLotOrder=self.sender),
            tc_rows_copy=Mock(return_value=[]),pump=Mock(),
            crosscheck_seq13=Mock(return_value=CrossCheckResult(ExecutionStatus.ACKNOWLEDGED,'1234567890123','1234567890123',accepted_rows=('ACK',))),
            ensure_ready=lambda:SimpleNamespace(ready=True,reply_state=1,reply_connected=True,action='REUSE'))

    def clock(self):
        at=self.at
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return at.astimezone(tz) if tz else at.astimezone().replace(tzinfo=None)
        return Clock

    def execute(self,payload,authorized=True,stopped=None):
        path=self.root/'advice.json';save(path,payload)
        with patch('execution.auto_advice_executor.datetime',self.clock()),patch('execution.auto_advice_executor.query_buying_power',return_value=self.balance):
            return execute_advice_file(path,project_root=self.root,config_path=self.cfg,session=self.session,
                account_snapshot=ExecutionAccountSnapshot(self.at.isoformat(),buying_power=self.balance),
                execution_db_path=self.store.path,health_order_test=authorized,stopped=stopped)

    def runtime(self):
        return SimpleNamespace(root=self.root,read_only=False,stopped=Mock(return_value=False),can_trade_now=Mock(return_value=True),
            snapshot=self.snapshot,refresh=Mock(return_value=self.snapshot),store=self.store,
            config=SimpleNamespace(max_quote_age_seconds=120),fetch_quotes=Mock(return_value={'0050':self.quote}),
            broker=SimpleNamespace(execute_health_order_test=Mock()))

    def test_confirmed_assumption_reaches_original_sdk_at_80_and_does_not_fake_a_fill(self):
        payload=build_payload(dict(self.quote),self.snapshot,self.at)
        first=self.execute(payload)
        self.assertEqual(first['results'][0]['status'],'ACKNOWLEDGED',first)
        self.sender.assert_called_once();order=self.sender.call_args.args[2]
        self.assertEqual((order.bstrStockNo,order.nQty,order.sBuySell,order.sPeriod,order.nTradeType),('0050',1,0,4,0))
        self.assertEqual(Decimal(order.bstrPrice),Decimal(80))  # Real ask=100; never rewrite the user's limit.
        self.assertEqual(order.nSpecialTradeType,2)
        self.assertEqual(self.store.get_ai_qty('0050'),0)
        again=self.execute(payload);self.assertEqual(again['results'][0]['status'],'SKIPPED_DUPLICATE')
        self.sender.assert_called_once();self.assertTrue(first['execution_timing']['events'])

    def test_ordinary_advice_cannot_enable_the_diagnostic_limit(self):
        result=self.execute(build_payload(self.quote,self.snapshot,self.at),authorized=False)
        self.sender.assert_not_called();self.assertIn('HEALTH_TEST_REQUIRES',result['results'][0]['reason'])

    def test_altered_contract_cannot_reach_sdk(self):
        base=build_payload(self.quote,self.snapshot,self.at)
        for field,value in (('symbol','2330'),('decision','SELL'),('suggested_qty',2),('suggested_price',81)):
            with self.subTest(field=field):
                payload=deepcopy(base);payload['recommendations'][0][field]=value
                with self.assertRaises(ValueError):self.execute(payload)
        self.sender.assert_not_called()

    def test_changed_preorder_trading_date_cannot_reach_sdk(self):
        payload=build_payload(None,self.snapshot,self.at)
        payload['valid_trading_date']='2026-11-14'
        with self.assertRaisesRegex(ValueError,'CONTRACT_CHANGED'):self.execute(payload)
        self.sender.assert_not_called()

    def test_original_cash_guard_ai_disable_and_stop_still_block_test_order(self):
        payload=build_payload(self.quote,self.snapshot,self.at)
        self.balance=BuyingPower(True,True,Decimal(5000),Decimal(5000),Decimal(5000),'ISOLATED')
        result=self.execute(payload);self.sender.assert_not_called();self.assertEqual(result['status'],'BLOCKED_BUDGET')
        self.balance=BuyingPower(True,True,Decimal(30000),Decimal(30000),Decimal(30000),'ISOLATED')
        with patch.object(ExecutionStore,'ai_trading_enabled',return_value=False):
            result=self.execute(payload);self.assertIn('disabled',result['results'][0]['reason'])
        self.execute(payload,stopped=lambda:True);self.sender.assert_not_called()

    def test_confirmation_is_required_and_closed_market_uses_user_price_without_fake_quotes(self):
        runtime=self.runtime()
        with self.assertRaisesRegex(ValueError,'CONFIRMATION'):run_test(runtime,{},confirmed=False)
        runtime.refresh.assert_not_called()
        runtime.can_trade_now.return_value=False
        self.at=self.at.replace(hour=17)
        def dispatch(path,stopped):
            import json
            payload=json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(payload['market']['quotes'][0]['quote_status'],'USER_FIXED_LIMIT')
            self.assertIsNone(payload['market']['quotes'][0]['ask'])
            return self.execute(payload,stopped=stopped)
        runtime.broker.execute_health_order_test.side_effect=dispatch
        self.snapshot['checked_at']=self.at.isoformat()
        with patch('opportunity_flow.health_order.datetime',self.clock()):report,_=run_test(runtime,{},confirmed=True)
        self.assertEqual(report['status'],'ACCEPTED',report);self.assertEqual(report['valid_trading_date'],'2026-11-16')
        runtime.fetch_quotes.assert_not_called();self.sender.assert_called_once()
        detail=self.store.events_for(report['decision_id'])[-1]['detail']
        self.assertEqual(detail['valid_trading_date'],'2026-11-16');self.assertTrue(detail['requested_outside_session'])
        self.assertEqual(self.store.expire_rod_orders(at=datetime(2026,11,16,9,tzinfo=TAIPEI)),[])
        self.at=datetime(2026,11,16,9,tzinfo=TAIPEI)
        with patch('execution.execution_store.datetime',self.clock()):
            self.assertEqual(self.store.daily_reserved_buy_twd(),Decimal(0))
        self.assertEqual(self.store.expire_rod_orders(at=datetime(2026,11,17,9,tzinfo=TAIPEI)),[])

    def test_full_diagnostic_handoff_uses_original_executor_and_formal_duplicate_guard(self):
        runtime=self.runtime()
        def dispatch(path,stopped):
            import json
            return self.execute(json.loads(path.read_text(encoding='utf-8')),stopped=stopped)
        runtime.broker.execute_health_order_test.side_effect=dispatch
        with patch('opportunity_flow.health_order.datetime',self.clock()):
            report,_=run_test(runtime,{},confirmed=True)
            again,_=run_test(runtime,{},confirmed=True)
        self.assertEqual(report['status'],'ACCEPTED',report);self.assertTrue(report['new_send_attempted'])
        self.assertTrue(again['duplicate']);self.assertFalse(again['new_send_attempted'])
        runtime.broker.execute_health_order_test.assert_called_once();self.sender.assert_called_once()
        self.assertEqual(report['actual_model_calls'],0);self.assertTrue(report['diagnostic_assumption'])
        self.assertTrue((self.root/'data/execution/health_order_test/latest.json').exists())

    def test_missing_or_stale_quote_and_pending_0050_do_not_dispatch(self):
        runtime=self.runtime()
        with patch('opportunity_flow.health_order.datetime',self.clock()):
            runtime.fetch_quotes.return_value={}
            report,_=run_test(runtime,{},confirmed=True);self.assertIn('ODDLOT_QUOTE_MISSING',report['reason'])
            runtime.fetch_quotes.return_value={'0050':dict(self.quote,quote_timestamp='2026-11-12T10:00:00+08:00')}
            report,_=run_test(runtime,{},confirmed=True);self.assertIn('QUOTE_STALE',report['reason'])
            self.store.claim('other-order','test','0050','BUY')
            report,_=run_test(runtime,{},confirmed=True);self.assertIn('UNRESOLVED',report['reason'])
        runtime.broker.execute_health_order_test.assert_not_called()

    def test_lost_response_stays_unconfirmed_and_acceptance_is_not_inferred_from_a_call(self):
        runtime=self.runtime();runtime.broker.execute_health_order_test.side_effect=TimeoutError('lost after dispatch')
        with patch('opportunity_flow.health_order.datetime',self.clock()):
            report,snapshot=run_test(runtime,{},confirmed=True)
            again,_=run_test(runtime,{},confirmed=True)
        self.assertEqual(report['status'],'UNCONFIRMED');self.assertIsNone(report['new_send_attempted'])
        self.assertEqual(again['status'],'UNCONFIRMED');self.assertTrue(again['duplicate'])
        self.assertEqual(runtime.broker.execute_health_order_test.call_count,1)
        findings={r['key']:r for r in checks(report,snapshot)}
        self.assertEqual(findings['diagnostic_submission']['status'],'WARN')
        self.assertEqual(findings['diagnostic_reconciliation']['status'],'WARN')


    def test_confirmed_real_fill_is_recorded_as_manual_without_changing_existing_ai_qty(self):
        self.store.bootstrap_ai_qty('0050',3)
        fields=['']*21
        fields[0],fields[1],fields[2],fields[3]='1234567890123','TC','D','N'
        fields[6],fields[8],fields[11],fields[20]='B','0050','79.95','1'
        self.session.crosscheck_seq13.return_value=CrossCheckResult(ExecutionStatus.FILLED,'1234567890123','1234567890123',fill_rows=(','.join(fields),))
        result=self.execute(build_payload(self.quote,self.snapshot,self.at))
        self.assertEqual(result['results'][0]['status'],'FILLED',result)
        self.assertEqual(self.store.get_ai_qty('0050'),3)
        self.execute(build_payload(self.quote,self.snapshot,self.at));self.sender.assert_called_once()
        self.assertEqual(self.store.get_ai_qty('0050'),3)

    def test_rejection_and_read_only_are_explicit_and_do_not_fake_acceptance(self):
        runtime=self.runtime();runtime.read_only=True
        with self.assertRaisesRegex(ValueError,'READ_ONLY'):run_test(runtime,{},confirmed=True)
        runtime.refresh.assert_not_called()
        self.sender.return_value=('broker rejected',1001)
        result=self.execute(build_payload(self.quote,self.snapshot,self.at));item=result['results'][0]
        self.assertEqual(item['status'],'FAILED');self.assertFalse(item['broker_order_sent'])
        report=dict(status='REJECTED',decision_generated=True,execution=result)
        self.assertEqual(checks(report,self.snapshot)[1]['status'],'FAIL')

    def test_diagnostic_flag_travels_on_existing_com_owner_without_enabling_normal_orders(self):
        import sys
        from execution.broker_runtime import BrokerRuntime
        with patch.dict(sys.modules,{'comtypes':SimpleNamespace(CoInitialize=Mock(),CoUninitialize=Mock())}),patch(
            'execution.broker_runtime.build_live_readonly_session_from_project',return_value=SimpleNamespace(connect=Mock())),patch(
            'execution.pipeline_hook.execute_after_advice_written',return_value={'status':'ISOLATED'}) as pipeline:
            runtime=BrokerRuntime(self.root)
            try:
                runtime.execute_health_order_test(self.root/'unused.json',lambda:False)
                self.assertIs(pipeline.call_args.kwargs['health_order_test'],True)
                runtime.execute(self.root/'unused.json')
                self.assertNotIn('health_order_test',pipeline.call_args.kwargs)
            finally:runtime.shutdown()

    def test_crash_after_claim_keeps_manual_ownership_in_the_first_durable_record(self):
        payload=build_payload(self.quote,self.snapshot,self.at)
        with patch.object(self.session,'ensure_ready',side_effect=KeyboardInterrupt('isolated interruption')):
            with self.assertRaises(KeyboardInterrupt):self.execute(payload)
        events=self.store.events_for(payload['recommendations'][0]['decision_id'])
        self.assertEqual(events[0]['status'],'STARTED')
        self.assertEqual(events[0]['detail']['ownership'],'MANUAL_DIAGNOSTIC')
        self.assertEqual(events[0]['detail']['required_twd_with_buffer'],'100')
        self.assertNotIn('0050',self.store.pending_reconciliation_symbols())
        self.sender.assert_not_called()

    def test_manual_diagnostic_does_not_use_ai_daily_or_allocation_allowance(self):
        self.snapshot.update(current_buy_budget_twd='0',budget_status='BLOCKED',strategy_cap_twd='0')
        save(self.cfg,dict(enabled=True,min_available_to_buy_twd=5000,min_buy_fee_reserve_twd=20,
            minimum_commission_twd=1,max_daily_buy_twd=1,observe_seconds=0))
        result=self.execute(build_payload(self.quote,self.snapshot,self.at))
        self.assertEqual(result['results'][0]['status'],'ACKNOWLEDGED',result)
        self.assertEqual(self.store.daily_reserved_buy_twd(),Decimal(0))
        self.sender.assert_called_once()


if __name__=='__main__':unittest.main()
