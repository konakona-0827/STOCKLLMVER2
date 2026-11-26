"""November 25: bounded resource use, 4743 frozen risk and automatic recovery."""
from copy import deepcopy
from concurrent.futures import Future
from datetime import timedelta
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from main import Audit
from opportunity_flow.common import digest,policy,read,stamp
from opportunity_flow.data import DatasetReader,load_dataset
from opportunity_flow.demo import create
from opportunity_flow.engine import build,economics,features
from opportunity_flow.execution_bridge import ExecutionBridge,FreshEconomicsError,validate_buy
from opportunity_flow.live import LiveCoordinator
from opportunity_flow.pipeline import run_cycle
from opportunity_flow.recovery import folder,intent,set_intent
from operations import flow_supervisor as supervisor
from operations.flow_resources import MemoryGuard,PRIVATE_LIMIT,pressure_reason,sample_memory
from test_flow_execution_bridge import FakeRuntime
from test_opportunity_flow import FakeRunner


class Economics4743Tests(unittest.TestCase):
    def setUp(self):
        self.at=stamp('2026-11-25T09:20:30+08:00');self.p=policy();self.p['minimum_commission_twd']=1
        self.context=dict(source='LIVE_ACCOUNT_CONTEXT',account_identity='isolated',held_qty=0,
            pricing_policy='PLANNED_LIMIT',risk_by_stop={},minimum_fee_reserve_twd=20,buy_cash_buffer_rate=.01)
        self.event=dict(status='SOURCE_REVIEW_REQUIRED',blockers=[],checked_at=self.at.isoformat(),source_url='https://openapi.twse.com.tw/')
        self.route=dict(route_id='frozen',eligible=True,entry=39.35,stop=dict(price=37.4),target=dict(price=44.4),
            economics=economics(39.35,37.4,44.4,20,.2,self.p),kind='CURRENT_ASK')
        packet=dict(symbol='4743',exchange='TPEX',decision_session=self.at.date().isoformat(),policy=self.p,
            execution_context=self.context,selected_routes=['frozen'],setup_families=['TREND_CONTINUATION'],
            evidence=[dict(evidence_id='E_FEATURES',facts={}),dict(evidence_id='E_LEVELS',facts={}),
                dict(evidence_id='E_ECONOMICS',facts=[self.route]),dict(evidence_id='E_DATA_LIMITS',facts=dict(event_check=self.event))])
        packet['packet_id']=digest(packet)
        opinion=dict(symbol='4743',packet_id=packet['packet_id'],verdict='RESEARCH',selected_route_id='frozen',
            evidence_ids=['E_ECONOMICS'],thesis='isolated reconstruction of archived numbers',counterevidence='risk if spread widens',
            wait_conditions=[],execution_missing=[])
        self.c=dict(symbol='4743',state='RESEARCH_READY',selected_route_id='frozen',review=dict(packet=packet,
            researcher=opinion,challenger=deepcopy(opinion),reviewed_at=(self.at-timedelta(seconds=122)).isoformat()))
        from opportunity_flow.skill_profile import load_profile
        self.c['review']['trading_skill']=load_profile().metadata
        self.q=dict(symbol='4743',exchange='TPEX',quote_status='LIVE',is_trial=False,source_market_number=6,
            quote_basis='capital_intraday_odd_lot',quote_timestamp=self.at.isoformat(),bid=39.15,ask=39.35,last_price=39.15,low=38.65)
        self.snapshot=dict(checked_at=self.at.isoformat(),status='OK',errors=[],budget_status='READY',current_buy_budget_twd=10000)

    def test_same_spread_uses_same_formula_but_one_tick_wider_exceeds_original_risk(self):
        self.assertEqual(validate_buy(self.c,self.q,self.snapshot,self.context,self.p,self.at,planned_limits=True),self.route)
        q={**self.q,'bid':39.1}
        with self.assertRaises(FreshEconomicsError) as caught:
            validate_buy(self.c,q,self.snapshot,self.context,self.p,self.at,planned_limits=True)
        detail=caught.exception.evidence
        self.assertEqual(detail['failed_checks'],['total_risk'])
        self.assertAlmostEqual(detail['researched']['modeled_loss_twd'],49.531)
        self.assertAlmostEqual(detail['fresh']['modeled_loss_twd'],50.53)
        self.assertEqual(self.route['economics']['quantity'],20)

    def test_failed_fresh_recalculation_records_values_before_model_or_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=FakeRuntime(self.p,self.at.isoformat(),{**self.q,'bid':39.1},self.context)
            runtime.config.planned_limits_enabled=True;runtime.disclosures={'4743':self.event};runtime.pretrade=Mock()
            bridge=ExecutionBridge(runtime,tmp,self.p,clock=lambda:self.at)
            result=bridge.step(dict(candidates=[self.c]),dict(grouped={'4743':[]}),self.at)
            block=result['blocked'][0]
            self.assertEqual(block['reason'],'FRESH_TRADE_ECONOMICS_FAILED')
            self.assertEqual(block['validation_phase'],'BEFORE_PRETRADE')
            self.assertAlmostEqual(block['economics_evidence']['fresh']['modeled_loss_twd'],50.53)
            self.assertFalse(runtime.sent);runtime.pretrade.assert_not_called()
            c=LiveCoordinator('unused',Path(tmp)/'coordinator',self.p,runtime=runtime)
            try:
                finished=Future();finished.set_result(result);c.future=finished;c.last_quote_refresh=123
                c.drain()
                self.assertTrue(c.research_refresh_required);self.assertEqual(c.last_quote_refresh,0)
                self.assertIsNone(c.future);self.assertFalse(runtime.sent)
            finally:c.close()

    def test_new_legal_quantity_changes_research_key_without_altering_approved_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            at=stamp(create(Path(tmp)/'data')).replace(hour=10);ds=load_dataset(Path(tmp)/'data')
            f=features(ds['grouped']['1111'],5);f.update(close=39.15,ma5=39,ma20=38,atr14=1)
            q={**self.q,'symbol':'1111','exchange':'TWSE','source_market_number':5,'quote_timestamp':at.isoformat()}
            context={**self.context,'available_cash_twd':10000}
            levels=([dict(price=37.4)],[dict(price=44.4)])
            with patch('opportunity_flow.engine.features',return_value=f),patch('opportunity_flow.engine.levels',return_value=levels):
                old=build('1111',ds,at,self.p,q,account=context)
                fresh=build('1111',ds,at,self.p,{**q,'bid':39.1},account=context)
            a=old['packet']['evidence'][2]['facts'][0];b=fresh['packet']['evidence'][2]['facts'][0]
            self.assertEqual(a['economics']['quantity'],20);self.assertEqual(b['economics']['quantity'],19)
            self.assertNotEqual(old['material_key'],fresh['material_key'])


class BoundedWorkTests(unittest.TestCase):
    def test_dataset_reuse_stays_locked_and_does_not_accept_changed_bad_history(self):
        from opportunity_flow.dataset_lock import dataset_lease
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';create(root);reader=DatasetReader(root);first=reader.get()
            with patch('opportunity_flow.data._load_dataset_locked',side_effect=AssertionError('Unchanged CSV reloaded')):
                for _ in range(30):self.assertIs(reader.get(),first)
            with dataset_lease(root):
                with self.assertRaisesRegex(ValueError,'DATASET_UPDATE_IN_PROGRESS'):reader.get()
            with (root/'bars.csv').open('a',encoding='utf-8') as stream:stream.write('\n')
            with self.assertRaisesRegex(ValueError,'DATASET_HASH_MISMATCH'):reader.get()

    def test_connection_audit_keeps_bounded_memory_and_all_events_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);audit=Audit(root,max_events=32)
            for i in range(500):audit.event('QUOTE',dict(index=i))
            self.assertEqual(len(audit.events),32)
            self.assertEqual(read(root/'connection.json')[0]['detail']['index'],468)
            archived=[json.loads(s) for path in root.glob('connection_*.jsonl') for s in path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(archived),500)
            self.assertEqual(archived[-1]['detail']['index'],499)

    def test_execution_copy_excludes_the_unapproved_whole_universe(self):
        class MustNotCopy:
            def __deepcopy__(self,memo):raise AssertionError('Copied a rejected stock history')
        with tempfile.TemporaryDirectory() as tmp:
            runtime=FakeRuntime(policy(),'2026-11-25T10:00:00+08:00',{},{});runtime.sent=[]
            c=LiveCoordinator('unused',tmp,policy(),runtime=runtime)
            try:
                with patch.object(c.pool,'submit',return_value=Mock()) as submit:
                    result=dict(candidates=[dict(symbol='1111',state='RESEARCH_READY')]+[
                        dict(symbol=str(i),state='RESEARCH_WATCH',large=MustNotCopy()) for i in range(1000)])
                    ds=dict(grouped={});c.offer(result,ds)
                    self.assertEqual(len(submit.call_args.args[1]['candidates']),1)
                    self.assertIs(submit.call_args.args[2],ds)
            finally:c.close()

    def test_background_review_builds_only_requested_stocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);at=create(root/'data');runner=FakeRunner()
            result=run_cycle(load_dataset(root/'data'),at,policy(),root/'review',use_llm=True,
                runner=runner,review_symbols={'1111'},focus_only=True,publish_latest=False)
            self.assertEqual(result['summary']['symbols_observed'],1)
            self.assertEqual([c['symbol'] for c in result['candidates']],['1111'])
            self.assertEqual(len(runner.calls),2)

    def test_periodic_account_refresh_runs_when_there_is_no_daily_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=FakeRuntime(policy(),'2026-11-25T10:00:00+08:00',{},{});runtime.refresh=Mock(side_effect=[
                TimeoutError('isolated timeout'),runtime.snapshot])
            c=LiveCoordinator('unused',tmp,policy(),runtime=runtime)
            try:
                c.last_refresh=0;c.maintenance(force=True);self.assertTrue(c.broker_read_error)
                c.last_refresh=0;c.maintenance(force=True)
                self.assertIsNone(c.broker_read_error);self.assertEqual(c.snapshot['budget_status'],'READY')
                self.assertIsNone(c.dataset);self.assertFalse(c.stop.is_set())
            finally:c.close()


class MemoryGuardTests(unittest.TestCase):
    def test_private_commit_and_system_commit_are_separate_from_resident_ram(self):
        self.assertEqual(pressure_reason(dict(private_commit_bytes=PRIVATE_LIMIT,working_set_bytes=1)),
            'PROCESS_MEMORY_BUDGET_EXCEEDED')
        self.assertEqual(pressure_reason(dict(private_commit_bytes=100,available_commit_bytes=100)),
            'SYSTEM_COMMIT_CRITICAL')
        self.assertIsNone(pressure_reason(None))

    def test_resource_sampling_is_bounded_and_errors_do_not_become_success(self):
        clock=[0];sampler=Mock(side_effect=[dict(private_commit_bytes=100),OSError('isolated')])
        guard=MemoryGuard(clock=lambda:clock[0],sampler=sampler)
        self.assertIsNone(guard.check(17));self.assertIsNone(guard.check(17));self.assertEqual(sampler.call_count,1)
        clock[0]=16;self.assertIsNone(guard.check(17));self.assertIsNone(guard.row)

    def test_guardian_requests_scoped_graceful_close_and_restarts_without_force_kill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);first=Mock(pid=171717);second=Mock(pid=181818)
            first.wait.side_effect=[subprocess.TimeoutExpired('isolated',5),0]
            second.wait.side_effect=lambda **kw:(set_intent(root,False,'USER_STOP') or 0)
            stop=threading.Event()
            with patch.object(supervisor,'RuntimeAudit'),patch.object(supervisor,'broker_owner_absent',return_value=True),\
                 patch('operations.flow_resources.sample_memory',return_value=dict(private_commit_bytes=PRIVATE_LIMIT)),\
                 patch.object(supervisor.subprocess,'Popen',side_effect=[first,second]) as launch,\
                 patch.object(stop,'wait',return_value=False):
                supervisor.supervise(root,root,root,stop=stop)
            request=read(folder(root)/'restart_request.json')
            self.assertEqual(request['reason'],'PROCESS_MEMORY_BUDGET_EXCEEDED');self.assertEqual(request['pid'],171717)
            self.assertEqual(launch.call_count,2)
            first.kill.assert_not_called();first.terminate.assert_not_called()
            self.assertFalse(intent(root))

    @unittest.skipUnless(os.name=='nt','Native Windows process counters')
    def test_native_private_commit_tracks_allocation_and_sampling_does_not_leak_handles(self):
        from operations.flow_resources import _windows_api
        import ctypes
        from ctypes import wintypes
        kernel,_,_,_=_windows_api()
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        kernel.GetProcessHandleCount.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        handles_before=wintypes.DWORD();handles_after=wintypes.DWORD()
        self.assertTrue(kernel.GetProcessHandleCount(kernel.GetCurrentProcess(),ctypes.byref(handles_before)))
        before=sample_memory(os.getpid());allocation=bytearray(32*1024**2)
        for i in range(0,len(allocation),4096):allocation[i]=1
        after=sample_memory(os.getpid())
        self.assertGreaterEqual(after['private_commit_bytes']-before['private_commit_bytes'],24*1024**2)
        self.assertGreater(after['working_set_bytes'],0)
        for _ in range(50):sample_memory(os.getpid())
        self.assertTrue(kernel.GetProcessHandleCount(kernel.GetCurrentProcess(),ctypes.byref(handles_after)))
        self.assertLessEqual(handles_after.value,handles_before.value+1)
        self.assertEqual(_windows_api.cache_info().currsize,1)
        del allocation;gc.collect()


if __name__=='__main__':unittest.main()
