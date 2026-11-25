"""A recovered watch loop reaches research, pretrade, fills and automatic exits."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime,timedelta
from decimal import Decimal
import io
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from opportunity_flow.common import save
from opportunity_flow.live import LiveCoordinator
import test_flow_execution_bridge as bridge_fixture
from test_opportunity_flow import FakeRunner


class RecoveryLifecycleTests(unittest.TestCase):
    def test_reconcile_before_research_then_buy_fill_and_exit_without_manual_dispatch(self):
        self.recovered_lifecycle()

    def test_rc9999_rebuild_reconciles_then_automatically_buys_manages_and_sells(self):
        self.recovered_lifecycle(reset_reply=True)

    def test_memory_rollover_reconciles_then_automatically_buys_manages_and_sells(self):
        self.recovered_lifecycle(memory_restart=True)

    def test_expired_account_refreshes_before_research_and_automatic_buy_sell(self):
        self.recovered_lifecycle(expired=True)

    def recovered_lifecycle(self,reset_reply=False,memory_restart=False,expired=False):
        from execution.account_guard import BuyingPower,InventoryRow
        from execution.account_snapshot import ExecutionAccountSnapshot
        from execution.auto_advice_executor import execute_advice_file
        from execution.capital_executor import SendResult
        from execution.execution_store import ExecutionStore
        from execution.models import ExecutionStatus,Side
        from opportunity_flow.__main__ import main
        fixture=bridge_fixture.BridgeTests('test_approved_research_reaches_existing_interface_without_chasing')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        root=fixture.root/'restart';root.mkdir();at=fixture.now
        store=ExecutionStore(root/'isolated.sqlite3')
        cfg=root/'config.json';save(cfg,dict(enabled=True,min_available_to_buy_twd=5000,
            min_buy_fee_reserve_twd=20,minimum_commission_twd=1,max_daily_buy_twd=30000,observe_seconds=0))
        runtime=fixture.runtime;runtime.store=store
        stages=[];verified=[False];sdk=[]
        original_refresh=runtime.refresh
        def refresh():
            if not stages:
                stages.append('BROKER_READ_FAILED')
                raise TimeoutError('simulated broker unavailable after reboot')
            verified[0]=True;stages.append('FRESH_ACCOUNT_VERIFIED')
            return original_refresh()
        runtime.refresh=refresh
        class Runner(FakeRunner):
            def __call__(self,packets,role):
                self_test.assertTrue(verified[0],'A restored file alone cannot authorize research execution')
                stages.append(role)
                return super().__call__(packets,role)
        self_test=self
        runner=Runner();pretrade=runtime.pretrade
        def review(decisions,candidates):
            stages.extend('PRETRADE_'+d['decision'] for d in decisions)
            return pretrade(decisions,candidates)
        runtime.pretrade=review
        def exits(candidates):
            stages.append('POSITION_MANAGEMENT')
            rows=[dict(symbol=c['symbol'],decision='SELL',confidence=0,action_ratio=1,
                suggested_qty=c['paper_position']['qty'],reference_price=c['last_price'],suggested_price=c['bid'],
                strategy_id='NONE',target_price=None,invalidation_price=None,holding_days=None,
                reason='isolated risk exit',data_quality='LIVE',warnings=[]) for c in candidates]
            return dict(market_view='test risk exit',decisions=rows),dict(validation='VALID',
                management_plans=[dict(symbol=c['symbol'],action='SELL',next_review_seconds=60) for c in candidates])
        runtime.review_exits=exits
        session=SimpleNamespace(connected=True,ensure_ready=lambda:SimpleNamespace(
            ready=True,reply_state=1,reply_connected=True,action='REUSE'))
        power=BuyingPower(True,True,Decimal(30000),Decimal(30000),Decimal(30000),'isolated')
        began=time.monotonic()
        instant=lambda:at+timedelta(seconds=time.monotonic()-began)
        from opportunity_flow.recovery import RecoveryProbe
        probe=RecoveryProbe(root,'lifecycle',clock=lambda:instant().timestamp())
        self.enterContext(patch('opportunity_flow.recovery.get_probe',return_value=probe))
        runtime.config.enabled=True
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):
                moment=instant()
                return moment.astimezone(tz) if tz else moment.astimezone().replace(tzinfo=None)
        def sender(**kwargs):
            self.assertTrue(verified[0]);sdk.append(kwargs['side']);stages.append('SDK_'+kwargs['side'].value)
            sequence=str(len(sdk)).zfill(13)
            kwargs['on_submitted'](SendResult(0,'accepted',sequence,'isolated'))
            return SimpleNamespace(symbol='1111',side=kwargs['side'],status=ExecutionStatus.FILLED,
                quantity=kwargs['quantity'],price=kwargs['price'],send_return_code=0,seq13=sequence,
                filled_quantity=kwargs['quantity'],broker_order_sent=True,send_attempted=True,
                crosscheck_performed=True,used_fallback=False,changes_position=True,
                crosscheck=SimpleNamespace(status=ExecutionStatus.FILLED,accepted_rows=['accepted'],
                                          fill_rows=[],cancel_rows=[],used_fallback=False))
        def execute(payload,path):
            runtime.sent.append(deepcopy(payload));save(path,payload)
            total=(store.get_ai_qty('1111') or 0)+3  # Three manual shares are never handed to AI.
            account=ExecutionAccountSnapshot(at.isoformat(),buying_power=power,
                inventory=(InventoryRow('1111','T',total,0,0,0,0,total,total,''),))
            result=execute_advice_file(path,project_root=root,config_path=cfg,session=session,
                                      account_snapshot=account,execution_db_path=store.path)
            self.assertEqual(result['results'][0]['status'],'FILLED',result)
            runtime.positions.clear();runtime.positions.update(store.ai_positions())
            return result
        runtime.execute=execute
        stop=threading.Event();wait=stop.wait;polls=[]
        coordinator=LiveCoordinator('unused',root/'watch',fixture.p,stop,runtime=runtime)
        with self.assertRaises(TimeoutError):coordinator.refresh_account()
        self.assertIsNone(coordinator.future)
        if reset_reply or memory_restart:
            if reset_reply:
                failed=dict(runtime.snapshot,status='ERROR',inventory_checked=False,budget_status='UNVERIFIED',
                    current_buy_budget_twd=None,errors=['session: CapitalReplySessionError: Reply reset failed: rc=9999'])
                with patch.object(runtime,'refresh',side_effect=lambda:deepcopy(failed)):
                    for _ in range(3):coordinator.refresh_account()
            else:probe.request_restart('PROCESS_MEMORY_BUDGET_EXCEEDED')
            self.assertTrue(probe.restart_requested())
            self.assertFalse(runtime.sent)
            coordinator.close()
            stop=threading.Event();wait=stop.wait
            probe=RecoveryProbe(root,'after-rc9999-rebuild',clock=lambda:instant().timestamp())
            self.enterContext(patch('opportunity_flow.recovery.get_probe',return_value=probe))
            self.assertFalse(probe.restart_requested())
            coordinator=LiveCoordinator('unused',root/'watch',fixture.p,stop,runtime=runtime)
        coordinator.bridge.now=instant
        if expired:
            from opportunity_flow.execution_bridge import verified_snapshot
            coordinator.snapshot=dict(runtime.snapshot,checked_at=(instant()-timedelta(seconds=121)).isoformat())
            with self.assertRaisesRegex(ValueError,'BROKER_ACCOUNT_STALE'):verified_snapshot(coordinator.snapshot,instant())
            coordinator.broker_read_error=None
        latest=[];step=coordinator.bridge.step
        def completed(*args,**kwargs):
            result=step(*args,**kwargs)
            if len(latest)<3:latest.append(result)
            return result
        coordinator.bridge.step=completed
        coordinator.last_refresh=0
        def tick(seconds):
            wait(.02);polls.append(seconds)
            if len(sdk)==1:coordinator.last_maintenance=0
            if len(sdk)>=2 or len(polls)>=80:stop.set()
        event=fixture.runtime.disclosures['1111']
        with patch('opportunity_flow.__main__.datetime',Clock),patch('opportunity_flow.live.datetime',Clock),\
             patch('execution.auto_advice_executor.datetime',Clock),\
             patch('execution.auto_advice_executor.query_buying_power',return_value=power),\
             patch('execution.auto_advice_executor.CapitalOddLotExecutor.send_limit_order',side_effect=sender),\
             patch('opportunity_flow.public_quotes.fetch',return_value=dict(quotes=[],errors=[],http_requests=1)),\
             patch('opportunity_flow.company_events.current_events',return_value={}),\
             patch('opportunity_flow.company_events.for_symbol',return_value=event),\
             patch('opportunity_flow.review.CodexRunner',return_value=runner),\
             patch.object(stop,'wait',side_effect=tick),redirect_stdout(io.StringIO()):
            main(['watch','--dataset',str(fixture.root/'data'),'--state',str(coordinator.state),
                  '--live','--llm','--public-quotes','--poll-seconds','1'],
                 coordinator=coordinator,external_stop=stop)
        self.assertEqual(sdk,[Side.BUY,Side.SELL],dict(stages=stages[:16],
            latest=[{k:v for k,v in r.items() if k in ('reports','blocked')} for r in latest]))
        self.assertEqual(store.get_ai_qty('1111'),0)
        self.assertLess(stages.index('FRESH_ACCOUNT_VERIFIED'),stages.index('researcher'))
        self.assertLess(stages.index('researcher'),stages.index('PRETRADE_BUY'))
        self.assertLess(stages.index('PRETRADE_BUY'),stages.index('SDK_BUY'))
        self.assertLess(stages.index('SDK_BUY'),stages.index('POSITION_MANAGEMENT'))
        self.assertLess(stages.index('POSITION_MANAGEMENT'),stages.index('PRETRADE_SELL'))
        self.assertLess(stages.index('PRETRADE_SELL'),stages.index('SDK_SELL'))
        observed=probe.row['stages']
        for stage in ('ACCOUNT_RECONCILED','WATCH_STARTED','RESEARCH_CYCLE_COMPLETED','RESEARCH_REVIEW_STARTED',
                      'RESEARCH_REVIEW_COMPLETED','PRETRADE_STARTED','PRETRADE_COMPLETED','POSITION_MANAGEMENT'):
            self.assertIn(stage,observed)
        self.assertEqual(observed['ORDER_SENT']['count'],2)
        self.assertEqual(observed['FILL_CONFIRMED']['count'],2)
        self.assertEqual(observed['FILL_CONFIRMED']['detail']['side'],'SELL')
        self.assertNotIn('isolated-test',str(probe.row['events']))


if __name__=='__main__':unittest.main()
