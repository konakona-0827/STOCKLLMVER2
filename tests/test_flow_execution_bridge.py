"""No real broker or model: exercise the new handoff and the actual legacy executor."""
from copy import deepcopy
from datetime import datetime,timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from opportunity_flow.common import policy,read,save,stamp
from opportunity_flow.data import load_dataset
from opportunity_flow.demo import create
from opportunity_flow.pipeline import run_cycle
from opportunity_flow.execution_bridge import ExecutionBridge,validate_buy,verified_snapshot
from test_opportunity_flow import FakeRunner


class FakeRuntime:
    def __init__(self,p,at,quote,context):
        self.p,self.at,self.quote,self.context=deepcopy(p),stamp(at),deepcopy(quote),deepcopy(context)
        self.read_only=False;self.support_policy='advisory';self.sent=[];self.positions={};self.pending=set();self.stop=False
        self.config=SimpleNamespace(planned_limits_enabled=False)
        self.snapshot=dict(checked_at=at,status='OK',errors=[],inventory_checked=True,reconciliation_errors=[],
            broker_inventory_verified=True,budget_status='READY',current_buy_budget_twd=10000,strategy_cap_twd=10000)
        self.store=SimpleNamespace(ai_positions=lambda:self.positions,pending_reconciliation_symbols=lambda:self.pending)
        self.fail_send=False;self.alter_review=False
        self.account_identity='isolated-test'
    def refresh(self):return self.snapshot
    def fetch_quotes(self,symbols,dataset):return {s:{**self.quote,'symbol':s} for s in symbols}
    def contexts(self,dataset,quotes):return {s:{**self.context,'held_qty':self.positions.get(s,0),'pending_execution':s in self.pending} for s in dataset['grouped']}
    def effective_policy(self,p):return self.p
    def account(self):return dict(available_cash_twd=float(self.snapshot['current_buy_budget_twd']),capital_limit_twd=10000,budget_status=self.snapshot['budget_status'])
    def candidates(self,symbols,quotes,dataset):
        return [dict(**quotes[s],asset_type='STOCK',paper_position=dict(qty=self.positions.get(s,0)),
            position=dict(ai_managed_qty=self.positions.get(s,0)),max_sell_qty=self.positions.get(s,0),
            pending_execution=s in self.pending,ai_cost_basis=dict(available=True,average_unit_cost_twd=100),buy_plans={}) for s in symbols]
    def pretrade(self,decisions,candidates):
        decisions=deepcopy(decisions)
        if self.alter_review and decisions:decisions[0]['suggested_qty']+=1
        return decisions,dict(status='VALID',reviews=[dict(symbol=d['symbol'],approved=True,reason='test') for d in decisions])
    def execute(self,payload,path):
        self.sent.append(payload)
        if self.fail_send:raise RuntimeError('lost reply after dispatch')
        return dict(status='TEST_EXECUTOR_ONLY',results=[])
    def stopped(self):return self.stop
    def can_trade_now(self):return True
    def close(self):pass
    def handoff_context(self,symbol,held):return None
    def review_exits(self,candidates):
        return dict(market_view='test exit',decisions=[dict(symbol=c['symbol'],decision='SELL',confidence=0,
            action_ratio=.5,suggested_qty=5,reference_price=c['last_price'],suggested_price=c['bid'],strategy_id='NONE',
            target_price=None,invalidation_price=None,holding_days=None,reason='user handoff reviewed against current trend',
            data_quality='LIVE',warnings=[]) for c in candidates]),dict(validation='VALID',management_plans=[
                dict(symbol=c['symbol'],action='SELL') for c in candidates])


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        day=create(self.root/'data');self.at=stamp(day).replace(hour=10).isoformat();self.now=stamp(self.at)+timedelta(seconds=2)
        self.ds=load_dataset(self.root/'data');self.p=policy()
        price=self.ds['grouped']['1111'][-1]['close']
        self.quote=dict(symbol='1111',exchange='TWSE',quote_status='LIVE',is_trial=False,
            quote_basis='capital_intraday_odd_lot',quote_timestamp=self.at,source_market_number=5,
            last_price=price,bid=price-.1,ask=price,source='CAPITAL_SKCOM')
        self.context=dict(source='LIVE_ACCOUNT_CONTEXT',account_identity='isolated-test',held_qty=0,
            available_cash_twd=10000,average_cost_twd=None,risk_by_stop={},trading_enabled=True,pending_execution=False,
            minimum_fee_reserve_twd=20,buy_cash_buffer_rate=.01)
        event=dict(status='SOURCE_REVIEW_REQUIRED',checked_at=self.at,source_url='https://openapi.twse.com.tw/',blockers=[])
        self.result=run_cycle(self.ds,self.at,self.p,self.root/'research',
            quotes={('TWSE','1111'):self.quote},events={'1111':event},runner=FakeRunner(),use_llm=True,
            account_context={'1111':self.context})
        self.c=next(c for c in self.result['candidates'] if c['symbol']=='1111')
        self.runtime=FakeRuntime(self.p,self.at,self.quote,self.context)
        self.bridge=ExecutionBridge(self.runtime,self.root/'live',self.p,clock=lambda:self.now)

    def validate(self,c=None,q=None,s=None,context=None,**kwargs):
        return validate_buy(c or self.c,q or self.quote,s or self.runtime.snapshot,context or self.context,self.p,self.now,**kwargs)

    def test_approved_research_reaches_existing_interface_without_chasing(self):
        result=self.bridge.step(self.result,self.ds,self.now)
        self.assertFalse(result['blocked'],result)
        self.assertEqual(len(self.runtime.sent),1)
        payload=self.runtime.sent[0]
        from execution.advice_adapter import build_advice_intents
        intents=build_advice_intents(payload)
        self.assertEqual(intents[0].side.value,'BUY')
        self.assertEqual(payload['recommendations'][0]['flow_plan']['limit_cap'],self.quote['ask'])
        self.assertEqual(payload['pretrade_review_status'],'VALID')

    def test_new_handoff_is_durable_across_bridge_restart(self):
        self.bridge.step(self.result,self.ds,self.now)
        payload=self.runtime.sent[0]
        other=ExecutionBridge(self.runtime,self.root/'live',self.p,clock=lambda:self.now)
        self.assertEqual(other.dispatch(payload)['status'],'SKIPPED_DUPLICATE_HANDOFF')
        self.assertEqual(len(self.runtime.sent),1)

    def test_lost_execution_reply_is_not_automatically_retried(self):
        self.runtime.fail_send=True
        result=self.bridge.step(self.result,self.ds,self.now)
        self.assertEqual(result['reports'][0]['status'],'DISPATCH_UNKNOWN_DO_NOT_RETRY')
        payload=self.runtime.sent[0]
        self.assertEqual(self.bridge.dispatch(payload)['status'],'SKIPPED_DUPLICATE_HANDOFF')

    def test_read_only_and_user_stop_never_call_sender(self):
        self.runtime.read_only=True
        self.bridge.step(self.result,self.ds,self.now)
        self.assertFalse(self.runtime.sent)
        self.runtime.read_only=False;self.runtime.stop=True
        self.bridge.step(self.result,self.ds,self.now)
        self.assertFalse(self.runtime.sent)

    def test_no_two_approvals_means_no_order(self):
        c=deepcopy(self.c);c['review']['challenger']['verdict']='WATCH'
        with self.assertRaisesRegex(ValueError,'TWO_RESEARCH'):self.validate(c=c)

    def test_position_change_after_research_requires_new_review(self):
        with self.assertRaisesRegex(ValueError,'POSITION_CHANGED'):
            self.validate(context={**self.context,'held_qty':1})

    def test_pending_broker_order_cannot_be_replaced_by_fresh_model_text(self):
        with self.assertRaisesRegex(ValueError,'PENDING_ORDER'):
            self.validate(context={**self.context,'pending_execution':True})

    def test_cash_reduction_is_rechecked_before_order(self):
        with self.assertRaisesRegex(ValueError,'CAPITAL_OR_ALLOCATION'):
            self.validate(s={**self.runtime.snapshot,'current_buy_budget_twd':1})

    def test_market_mismatch_stale_trial_and_public_quotes_fail(self):
        for update in ({'source_market_number':6},{'is_trial':True},{'quote_status':'PUBLIC_REFERENCE'},
                       {'quote_timestamp':(self.now-timedelta(seconds=121)).isoformat()}):
            with self.subTest(update=update),self.assertRaises(ValueError):self.validate(q={**self.quote,**update})

    def test_review_expiry_and_session_change_are_visible(self):
        c=deepcopy(self.c);c['review']['reviewed_at']=(self.now-timedelta(seconds=14401)).isoformat()
        with self.assertRaisesRegex(ValueError,'REVIEW_STALE'):self.validate(c=c)

    def test_unknown_events_and_strict_policy_do_not_become_certified(self):
        c=deepcopy(self.c);packet=c['review']['packet'];packet['evidence'][3]['facts']['event_check']['blockers']=['SOURCE_UNAVAILABLE']
        from opportunity_flow.common import digest
        packet['packet_id']=digest({k:v for k,v in packet.items() if k!='packet_id'})
        for role in ('researcher','challenger'):c['review'][role]['packet_id']=packet['packet_id']
        with self.assertRaisesRegex(ValueError,'EVENT_CHECK'):self.validate(c=c)
        with self.assertRaisesRegex(ValueError,'STRICT_RESEARCH'):self.validate(support_policy='confirmed_support')

    def test_pretrade_cannot_change_reviewed_quantity(self):
        self.runtime.alter_review=True
        result=self.bridge.step(self.result,self.ds,self.now)
        self.assertIn('PRETRADE_ALTERED_FROZEN_PLAN',[x['reason'] for x in result['blocked']])
        self.assertFalse(self.runtime.sent)

    def test_above_entry_waits_in_current_non_planned_mode(self):
        with self.assertRaisesRegex(ValueError,'WAITING_FOR_REVIEWED_ENTRY'):
            self.validate(q={**self.quote,'ask':self.quote['ask']+.5})

    def test_verified_sell_is_not_blocked_by_zero_buy_allowance(self):
        snapshot={**self.runtime.snapshot,'budget_status':'BLOCKED','current_buy_budget_twd':0}
        verified_snapshot(snapshot,self.now,need_buying_power=False)
        with self.assertRaisesRegex(ValueError,'ALLOWANCE'):verified_snapshot(snapshot,self.now)

    def test_full_bridge_sells_held_position_without_any_new_buy_candidate(self):
        self.runtime.positions={'1111':10};self.runtime.snapshot.update(budget_status='BLOCKED',current_buy_budget_twd=0)
        self.bridge.step(dict(candidates=[]),self.ds,self.now)
        self.assertEqual(len(self.runtime.sent),1)
        r=self.runtime.sent[0]['recommendations'][0]
        self.assertEqual((r['decision'],r['suggested_qty'],r['ai_managed_qty']),('SELL',5,10))

    def test_stale_quote_after_exit_model_does_not_reach_executor(self):
        self.runtime.positions={'1111':10}
        original=self.runtime.review_exits
        def review(candidates):
            result=original(candidates);self.runtime.quote['quote_status']='UNAVAILABLE';return result
        self.runtime.review_exits=review
        result=self.bridge.step(dict(candidates=[]),self.ds,self.now)
        self.assertFalse(self.runtime.sent)
        self.assertTrue(result['blocked'])

    def test_normal_maintenance_does_not_starve_new_buy_work_with_exit_only_jobs(self):
        from opportunity_flow.live import LiveCoordinator
        coordinator=LiveCoordinator('unused',self.root/'coordinator',self.p,runtime=self.runtime)
        coordinator.dataset=self.ds
        coordinator.bridge.step=Mock(return_value=dict(reports=[],blocked=[]))
        self.runtime.positions={'1111':10}
        try:
            coordinator.maintenance()
            self.assertIsNone(coordinator.future)
            coordinator.offer(self.result,self.ds)
            coordinator.future.result(timeout=2)
            self.assertEqual(coordinator.bridge.step.call_args.args[0]['candidates'],self.result['candidates'])
        finally:coordinator.close()

    def test_ready_research_is_queued_while_position_worker_is_busy(self):
        from concurrent.futures import Future
        from opportunity_flow.live import LiveCoordinator
        coordinator=LiveCoordinator('unused',self.root/'queued',self.p,runtime=self.runtime)
        busy=Future();coordinator.future=busy
        coordinator.bridge.step=Mock(return_value=dict(reports=[],blocked=[]))
        try:
            coordinator.offer(self.result,self.ds)
            self.assertIsNotNone(coordinator.pending)
            busy.set_result(dict(reports=[],blocked=[]))
            coordinator.drain();coordinator.future.result(timeout=2)
            self.assertEqual(coordinator.bridge.step.call_args.args[0]['candidates'],self.result['candidates'])
        finally:coordinator.close()

    def test_exit_model_failure_does_not_abort_independent_new_buy_research(self):
        # A bad existing-position model response is surfaced, while another stock retains its BUY path.
        self.runtime.positions={'2222':10}
        self.runtime.review_exits=Mock(side_effect=ValueError('bad model JSON'))
        result=self.bridge.step(self.result,self.ds,self.now)
        self.assertEqual(len(self.runtime.sent),1)
        self.assertEqual(self.runtime.sent[0]['recommendations'][0]['symbol'],'1111')
        self.assertTrue(any('POSITION_REVIEW_ERROR' in b['reason'] for b in result['blocked']))

    def test_one_position_price_change_does_not_repeat_other_position_model_calls(self):
        self.runtime.positions={'1111':10,'2222':10}
        self.runtime.review_exits=Mock(wraps=self.runtime.review_exits)
        self.bridge.step(dict(candidates=[]),self.ds,self.now)
        self.assertEqual(self.runtime.review_exits.call_count,2)
        original=self.runtime.fetch_quotes
        def quotes(symbols,dataset):
            result=original(symbols,dataset)
            if '2222' in result:
                for field in ('last_price','bid','ask'):result['2222'][field]+=1
            return result
        self.runtime.fetch_quotes=quotes
        self.bridge.step(dict(candidates=[]),self.ds,self.now)
        self.assertEqual(self.runtime.review_exits.call_count,3)
        self.assertEqual(self.runtime.review_exits.call_args.args[0][0]['symbol'],'2222')

    def test_sell_with_bid_only_quote_still_reaches_handoff(self):
        self.runtime.positions={'1111':10};self.runtime.quote['ask']=None
        self.bridge.step(dict(candidates=[]),self.ds,self.now)
        self.assertEqual(len(self.runtime.sent),1)
        self.assertIsNone(self.runtime.sent[0]['market']['quotes'][0]['ask'])

    def test_stop_after_model_and_outside_market_never_dispatch(self):
        self.runtime.positions={'1111':10};original=self.runtime.review_exits
        def stopped_after(candidates):
            response=original(candidates);self.runtime.stop=True;return response
        self.runtime.review_exits=stopped_after
        self.bridge.step(dict(candidates=[]),self.ds,self.now)
        self.assertFalse(self.runtime.sent)
        self.runtime.stop=False;self.runtime.can_trade_now=lambda:False
        self.bridge.step(self.result,self.ds,self.now)
        self.assertFalse(self.runtime.sent)

    def test_daily_download_failure_keeps_exit_maintenance_alive(self):
        import io,threading
        from contextlib import redirect_stdout
        from opportunity_flow.__main__ import main
        from opportunity_flow.live import LiveCoordinator
        from opportunity_flow.state import Ledger
        stop=threading.Event();wait=stop.wait;polls=[]
        coordinator=LiveCoordinator('unused',self.root/'downloader',self.p,stop,runtime=self.runtime)
        coordinator.dataset=self.ds;self.runtime.positions={'1111':10}
        coordinator.bridge.step=Mock(return_value=dict(reports=[],blocked=[]))
        def tick(seconds):
            wait(.03);polls.append(seconds)
            if len(polls)>=4:stop.set()
        with patch.object(stop,'wait',side_effect=tick),patch('opportunity_flow.collector.collect',side_effect=RuntimeError('offline')) as collect,\
             patch('opportunity_flow.__main__.refresh_due',return_value='2026-09-30'),redirect_stdout(io.StringIO()):
            main(['watch','--dataset',str(self.root/'data'),'--state',str(coordinator.state),'--llm','--live','--refresh-daily'],
                coordinator=coordinator,external_stop=stop)
        collect.assert_called_once()  # Retry backoff prevents a request storm.
        self.assertGreaterEqual(coordinator.bridge.step.call_count,1)
        ledger=Ledger(coordinator.state)
        try:self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM feed_events WHERE kind='DAILY_REFRESH_ERROR'").fetchone()[0],1)
        finally:ledger.close()

    def test_actual_existing_executor_sells_only_confirmed_ai_quantity(self):
        from execution.account_guard import InventoryRow
        from execution.account_snapshot import ExecutionAccountSnapshot
        from execution.auto_advice_executor import execute_advice_file
        from execution.execution_store import ExecutionStore
        from execution.models import ExecutionStatus,Side
        from execution.capital_executor import SendResult
        self.runtime.positions={'1111':10};self.runtime.snapshot.update(budget_status='BLOCKED',current_buy_budget_twd=0)
        self.bridge.step(dict(candidates=[]),self.ds,self.now);payload=self.runtime.sent[0]
        root=self.root/'sell';store=ExecutionStore(root/'isolated.sqlite3');store.bootstrap_ai_qty('1111',10)
        cfg=root/'execution.json';save(cfg,dict(enabled=True,observe_seconds=0))
        path=root/'advice.json';save(path,payload)
        snapshot=ExecutionAccountSnapshot(self.at,inventory=(InventoryRow('1111','T',30,0,0,0,0,30,30,''),))
        session=SimpleNamespace(connected=True,ensure_ready=lambda:SimpleNamespace(ready=True,reply_state=1,reply_connected=True,action='REUSE'))
        at=self.now
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return at.astimezone(tz) if tz else at.astimezone().replace(tzinfo=None)
        def send(**kwargs):
            self.assertEqual(kwargs['quantity'],5)
            kwargs['on_submitted'](SendResult(0,'accepted','1234567890123','test'))
            return SimpleNamespace(symbol='1111',side=Side.SELL,status=ExecutionStatus.PARTIALLY_FILLED,quantity=5,price=kwargs['price'],
                send_return_code=0,seq13='1234567890123',filled_quantity=2,broker_order_sent=True,
                send_attempted=True,crosscheck_performed=True,used_fallback=False,changes_position=True,
                crosscheck=SimpleNamespace(status=ExecutionStatus.PARTIALLY_FILLED,accepted_rows=['accepted'],
                    fill_rows=['confirmed two'],cancel_rows=[],used_fallback=False))
        with patch('execution.auto_advice_executor.datetime',Clock),\
             patch('execution.auto_advice_executor.CapitalOddLotExecutor.send_limit_order',side_effect=send) as sender:
            report=execute_advice_file(path,project_root=root,config_path=cfg,session=session,account_snapshot=snapshot,execution_db_path=store.path)
        self.assertEqual(report['results'][0]['status'],'PARTIALLY_FILLED',report)
        self.assertEqual(store.get_ai_qty('1111'),8)  # Manual 20 shares stay outside AI ownership.
        sender.assert_called_once()

    def test_actual_existing_executor_records_only_confirmed_partial_fill(self):
        from execution.auto_advice_executor import execute_advice_file
        from execution.account_guard import BuyingPower
        from execution.account_snapshot import ExecutionAccountSnapshot
        from execution.execution_store import ExecutionStore
        from execution.models import ExecutionStatus,Side
        from execution.capital_executor import SendResult
        self.bridge.step(self.result,self.ds,self.now);payload=self.runtime.sent[0]
        root=self.root/'executor';root.mkdir();store=ExecutionStore(root/'isolated.sqlite3')
        cfg=root/'config.json';save(cfg,dict(enabled=True,min_available_to_buy_twd=0,min_buy_fee_reserve_twd=20,
            minimum_commission_twd=1,max_daily_buy_twd=30000,observe_seconds=0))
        advice=root/'advice.json';save(advice,payload)
        balance=BuyingPower(True,False,Decimal(30000),Decimal(30000),Decimal(30000),'test')
        snapshot=ExecutionAccountSnapshot(self.at,buying_power=balance)
        session=SimpleNamespace(connected=True,ensure_ready=lambda:SimpleNamespace(ready=True,reply_state=1,reply_connected=True,action='REUSE'))
        at=self.now
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return at.astimezone(tz) if tz else at.astimezone().replace(tzinfo=None)
        def send(**kwargs):
            kwargs['on_submitted'](SendResult(0,'accepted','1234567890123','test'))
            return SimpleNamespace(symbol='1111',side=Side.BUY,status=ExecutionStatus.PARTIALLY_FILLED,
                quantity=kwargs['quantity'],price=kwargs['price'],send_return_code=0,seq13='1234567890123',
                filled_quantity=2,broker_order_sent=True,send_attempted=True,crosscheck_performed=True,
                changes_position=True,used_fallback=False,crosscheck=SimpleNamespace(status=ExecutionStatus.PARTIALLY_FILLED,
                    accepted_rows=['ack'],fill_rows=['two shares'],cancel_rows=[],used_fallback=False))
        with patch('execution.auto_advice_executor.datetime',Clock),\
             patch('execution.auto_advice_executor.query_buying_power',return_value=balance),\
             patch('execution.auto_advice_executor.CapitalOddLotExecutor.send_limit_order',side_effect=send) as sender:
            result=execute_advice_file(advice,project_root=root,config_path=cfg,session=session,account_snapshot=snapshot,execution_db_path=store.path)
            self.assertEqual(result['results'][0]['status'],'PARTIALLY_FILLED',result)
            self.assertEqual(store.get_ai_qty('1111'),2)
            again=execute_advice_file(advice,project_root=root,config_path=cfg,session=session,account_snapshot=snapshot,execution_db_path=store.path)
            self.assertEqual(again['results'][0]['status'],'SKIPPED_DUPLICATE')
            sender.assert_called_once()


class HandoffAndSourceTests(unittest.TestCase):
    def test_resume_after_month_end_does_not_reuse_partial_old_calendar(self):
        from opportunity_flow.collector import collect
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);sources=folder/'sources';sources.mkdir()
            save(sources/'calendar_202610_20261010.json',dict(stat='OK',data=[['115/10/09']]))
            save(sources/'calendar_202610_20261010.meta.json',dict(retrieved_at='2026-10-10T18:00:00+08:00'))
            class HTTP:
                def __init__(self):self.folder=sources;self.records=[];self.names=[]
                def get(self,name,url):
                    self.names.append(name)
                    record=dict(cache_hit=True,sha256='isolated',retrieved_at='2026-11-04T18:00:00+08:00')
                    self.records.append(record)
                    if name.startswith('master_TWSE'):payload=[{'公司代號':'1111','公司簡稱':'test'}]
                    elif name.startswith('master_TPEX'):payload=[dict(SecuritiesCompanyCode='2222',CompanyAbbreviation='test')]
                    elif name.startswith('calendar_202611'):payload=dict(stat='OK',data=[['115/11/02'],['115/11/03']])
                    elif name=='calendar_202610_20261010':payload=dict(stat='OK',data=[['115/10/09']])
                    elif name.startswith('calendar_202610'):payload=dict(stat='OK',data=[[f'115/10/{d:02}'] for d in (20,21,22,23,26,27,28,29,30)])
                    else:
                        exchange,session=name.split('_',1);tw=exchange=='TWSE'
                        fields=['證券代號','開盤價','最高價','最低價','收盤價','成交股數','成交金額'] if tw else ['代號','開盤','最高','最低','收盤','成交股數','成交金額(元)']
                        payload=dict(date=session.replace('-',''),stat='OK',tables=[dict(title='上市股票' if tw else '上櫃股票行情',
                            fields=fields,data=[['1111' if tw else '2222','100','102','99','101','100000','10000000']])])
                    return payload,record
            http=HTTP();collect(folder,'2026-11-03',10,http=http)
            ds=load_dataset(folder)
            self.assertNotIn('calendar_202610_20261010',http.names)
            self.assertIn('2026-10-30',ds['sessions']);self.assertEqual(len(ds['sessions']),10)

    def test_collection_os_lock_blocks_inconsistent_reads_but_stale_marker_recovers(self):
        from opportunity_flow.dataset_lock import dataset_lease
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)/'data';create(folder)
            save(folder/'.collecting',dict(lock_version=1,started_at='interrupted process'))
            self.assertEqual(len(load_dataset(folder)['grouped']),3)
            with dataset_lease(folder):
                with self.assertRaisesRegex(ValueError,'DATASET_UPDATE_IN_PROGRESS'):load_dataset(folder)
            self.assertEqual(len(load_dataset(folder)['grouped']),3)

    def test_stop_at_sdk_boundary_blocks_order_after_session_health_check(self):
        from execution.capital_executor import CapitalOddLotExecutor,CapitalExecutionError
        from execution.models import Side
        session=SimpleNamespace(ensure_ready=Mock(),tc_rows_copy=lambda:[],account='ISOLATED_TEST',
            _sk=SimpleNamespace(STOCKORDER=lambda:SimpleNamespace()),_skO=SimpleNamespace(SendStockOddLotOrder=Mock()))
        executor=CapitalOddLotExecutor(session);executor.stopped=lambda:True
        with self.assertRaisesRegex(CapitalExecutionError,'USER_STOPPED_BEFORE_SDK_SEND'):
            executor.send_limit_order(symbol='1111',side=Side.BUY,quantity=1,price=100,observe_seconds=0)
        session.ensure_ready.assert_called_once();session._skO.SendStockOddLotOrder.assert_not_called()

    def test_transfer_prefers_verified_broker_cost_and_requires_manual_fallback(self):
        import threading
        from opportunity_flow.legacy_runtime import LegacyRuntime
        runtime=LegacyRuntime.__new__(LegacyRuntime)
        runtime.read_only=False;runtime.stop_event=None;runtime.operation_lock=threading.RLock();runtime.root=Path('isolated')
        runtime.snapshot=dict(inventory_comparison=[dict(symbol='1815',broker_cash_qty=10)],ai_positions={},verified_position_costs={})
        runtime.refresh=lambda:runtime.snapshot
        with patch('execution.position_configuration.save_ai_holding_configuration',return_value={}) as save_holding:
            with self.assertRaisesRegex(ValueError,'成本'):runtime.transfer('1815',5)
            save_holding.assert_not_called()
            runtime.transfer('1815',5,'129.5')
            self.assertEqual(save_holding.call_args.kwargs['handoff_source'],'USER_INPUT')
            runtime.snapshot['verified_position_costs']['1815']=dict(verified=True,quantity=10,average_unit_cost_twd='125')
            runtime.transfer('1815',5,'999')
            self.assertEqual(save_holding.call_args.kwargs['unit_cost_twd'],'125')
            self.assertEqual(save_holding.call_args.kwargs['handoff_source'],'VERIFIED_BROKER_COST')
            runtime.read_only=True
            with self.assertRaises(ValueError):runtime.transfer('1815',5,'125')

    def test_explicit_venue_routing_isolates_failure_and_never_guesses_unknown_market(self):
        from execution.broker_runtime import BrokerRuntime
        runtime=BrokerRuntime.__new__(BrokerRuntime)
        def submit(kind,symbols,audit,stopped,timeout,market):
            if market==5:raise RuntimeError('listed market unavailable')
            return {s:dict(venue='TPEx') for s in symbols},{}
        runtime._submit=Mock(side_effect=submit)
        quotes,errors=runtime.fetch_quotes_with_markets(['2330','1815','9999'],{'2330':5,'1815':6},None,lambda:False)
        self.assertEqual(set(quotes),{'1815'})
        self.assertIn('2330',errors);self.assertEqual(errors['9999'],'ODDLOT_MARKET_UNKNOWN')
        self.assertEqual([c.args[-1] for c in runtime._submit.call_args_list],[5,6])

    def test_stopped_collector_releases_lock_and_never_starts_http(self):
        from opportunity_flow.collector import collect
        with tempfile.TemporaryDirectory() as temp,patch('opportunity_flow.collector.urlopen') as http:
            with self.assertRaisesRegex(RuntimeError,'COLLECTION_STOPPED'):collect(temp,'2026-09-30',1,stopped=lambda:True)
            self.assertFalse((Path(temp)/'.collecting').exists());http.assert_not_called()

    def test_corrupt_cost_history_is_marked_unavailable_without_crashing_position_review(self):
        from opportunity_flow.legacy_runtime import LegacyRuntime
        from execution.execution_store import ExecutionStore
        with tempfile.TemporaryDirectory() as temp:
            runtime=LegacyRuntime.__new__(LegacyRuntime);runtime.store=ExecutionStore(Path(temp)/'cost.sqlite3')
            with runtime.store._connect() as con:
                con.execute('CREATE TABLE manual_ai_handoffs(id INTEGER,symbol TEXT,previous_ai_qty INTEGER,quantity INTEGER,unit_cost_twd TEXT,cost_source TEXT,transferred_at TEXT)')
                con.execute("INSERT INTO manual_ai_handoffs VALUES(1,'1815',0,10,'100','USER_INPUT','bad date')")
            result=runtime.handoff_context('1815',10)
            self.assertIsNone(result['remaining_manual_unit_cost_twd']);self.assertFalse(result['original_buy_plan_known'])

    def test_manual_transfer_is_atomic_and_has_cost_and_unknown_plan_flag(self):
        from execution.execution_store import ExecutionStore
        from execution.position_configuration import save_ai_holding_configuration
        from opportunity_flow.legacy_runtime import LegacyRuntime
        from opportunity_flow.common import TAIPEI
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);cfg=root/'config/execution_live.json';save(cfg,dict(enabled=True,min_buy_fee_reserve_twd=20,buy_cash_buffer_rate=.01))
            store=ExecutionStore(root/'data/execution/live_execution.sqlite3')
            at=datetime.now(TAIPEI);snapshot=dict(checked_at=at.isoformat(),broker_inventory_verified=True,errors=[],
                inventory_comparison=[dict(symbol='1815',broker_cash_qty=30)],ai_positions={},ai_committed_positions=[])
            result=save_ai_holding_configuration(root,root/'data/analysis/market_history.sqlite3','1815',10,snapshot,
                unit_cost_twd='129.5',at=at,handoff_source='USER_INPUT')
            self.assertEqual(result['ai_qty'],10);self.assertEqual(result['user_qty'],20)
            runtime=LegacyRuntime.__new__(LegacyRuntime);runtime.store=store
            context=runtime.handoff_context('1815',10)
            self.assertFalse(context['original_buy_plan_known']);self.assertEqual(context['unit_cost_twd'],'129.5')
            with store._connect() as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM execution_attempts').fetchone()[0],0)
            with self.assertRaises(ValueError):
                save_ai_holding_configuration(root,root/'data/analysis/market_history.sqlite3','1815',40,snapshot,
                    unit_cost_twd=129.5,at=at,handoff_source='USER_INPUT')
            self.assertEqual(store.get_ai_qty('1815'),10)

    def test_missing_cost_and_nan_cannot_be_substituted_with_current_price(self):
        from opportunity_flow.control_panel import transfer_values
        for cost in ('','NaN','-1','0'):
            with self.subTest(cost=cost),self.assertRaises(ValueError):transfer_values('1815','10',cost)
        self.assertEqual(transfer_values('1815','10','129.5'),('1815',10,'129.5'))

    def test_company_action_dates_are_explicit_and_never_adjusted_price_certificates(self):
        from opportunity_flow.company_events import for_symbol,SOURCES
        at='2026-11-12T10:00:00+08:00'
        snapshot=dict(checked_at=at,sources=SOURCES,errors={},rows=dict(TPEX_NEWS=[],TPEX_ACTIONS=[
            dict(SecuritiesCompanyCode='1815',ExRrightsExDividendDate='1151113')]))
        result=for_symbol(snapshot,'1815','TPEX',at,5)
        self.assertIn('UPCOMING_CORPORATE_ACTION_REQUIRES_NEW_PLAN',result['blockers'])
        self.assertFalse(result['historical_corporate_actions_verified'])

    def test_control_panel_contains_editable_transfer_cost_without_starting_broker(self):
        import tkinter as tk
        from opportunity_flow.control_panel import ControlPanel
        root=tk.Tk();root.withdraw()
        try:
            panel=ControlPanel(root,SimpleNamespace(state='unused'),start=False)
            self.assertEqual(str(panel.cost_entry.cget('state')),'normal')
            self.assertIsNone(panel.coordinator)
        finally:root.destroy()

    def test_manual_average_cost_survives_partial_sell_and_second_transfer(self):
        from opportunity_flow.handoff_basis import remaining_manual_basis
        import json
        handoffs=[dict(previous_ai_qty=0,quantity=10,unit_cost_twd='100',transferred_at='2026-11-10T10:00:00+08:00'),
                  dict(previous_ai_qty=5,quantity=5,unit_cost_twd='120',transferred_at='2026-11-12T10:00:00+08:00')]
        attempts=[dict(side='SELL',started_at='2026-11-11T10:00:00+08:00',detail_json=json.dumps(dict(filled_quantity=5)))]
        self.assertEqual(remaining_manual_basis(handoffs,attempts,10),'110')


if __name__=='__main__':unittest.main()
