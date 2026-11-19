"""November 19 regressions: failed Reply reset, lost guardian and rotating disclosures."""
from copy import deepcopy
from datetime import datetime
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

import test_flow_execution_bridge as fixture_module
from test_opportunity_flow import FakeRunner
from opportunity_flow.common import read,save
from opportunity_flow.company_events import for_symbol,for_symbols,event_facts
from opportunity_flow.live import LiveCoordinator
from opportunity_flow.pipeline import run_cycle
from opportunity_flow.recovery import RecoveryProbe,folder,intent,set_intent
from operations import flow_supervisor as supervisor
from state_lock import StateLease


class FlowIncidentTests(unittest.TestCase):
    def setUp(self):
        fixture=fixture_module.BridgeTests('test_approved_research_reaches_existing_interface_without_chasing')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture;self.root=fixture.root/'incident';self.root.mkdir()
        self.runtime=fixture.runtime
        self.coordinator=LiveCoordinator('unused',self.root/'watch',fixture.p,threading.Event(),runtime=self.runtime)
        self.addCleanup(self.coordinator.close)
        self.probe=RecoveryProbe(self.root,'incident')
        self.enterContext(patch('opportunity_flow.recovery.get_probe',return_value=self.probe))

    def failed(self):
        return dict(self.runtime.snapshot,status='ERROR',inventory_checked=False,
            broker_inventory_verified=False,current_buy_budget_twd=None,budget_status='UNVERIFIED',
            errors=['session: CapitalReplySessionError: Reply reset failed: rc=9999'])

    def test_three_failed_resets_request_graceful_process_rebuild_and_block_old_work(self):
        self.runtime.refresh=Mock(side_effect=lambda:deepcopy(self.failed()))
        self.coordinator.pending=('old-advice',{})
        for _ in range(2):
            self.coordinator.refresh_account()
            self.assertFalse(self.probe.restart_requested())
        self.coordinator.refresh_account()
        self.assertTrue(self.probe.restart_requested())
        self.assertEqual(read(folder(self.root)/'restart_request.json')['reason'],'BROKER_REPLY_RESET_FAILED')
        self.assertTrue(intent(self.root))
        self.assertIsNone(self.coordinator.pending)
        self.assertTrue(self.coordinator.broker_read_error)
        self.coordinator.offer(self.fixture.result,self.fixture.ds)
        self.assertIsNone(self.coordinator.future)
        self.assertFalse(self.runtime.sent)

    def test_success_resets_failure_streak_but_failed_account_never_clears_block(self):
        good=deepcopy(self.runtime.snapshot)
        failed=self.failed()
        self.runtime.refresh=Mock(side_effect=[deepcopy(failed),good,deepcopy(failed),deepcopy(failed)])
        self.coordinator.refresh_account();self.assertTrue(self.coordinator.broker_read_error)
        self.coordinator.refresh_account();self.assertIsNone(self.coordinator.broker_read_error)
        self.coordinator.refresh_account();self.coordinator.refresh_account()
        self.assertEqual(self.coordinator.session_reset_failures,2)
        self.assertFalse(self.probe.restart_requested())
        self.runtime.refresh.side_effect=None
        self.runtime.refresh.return_value=dict(good,reconciliation_errors=['AI_EXCEEDS_BROKER'])
        self.coordinator.refresh_account()
        self.assertTrue(self.coordinator.broker_read_error)

    def test_low_cash_and_unknown_orders_do_not_trigger_a_session_reset(self):
        snapshot=dict(self.runtime.snapshot,current_buy_budget_twd='0',budget_status='BLOCKED',
                      buy_block_reasons=['MINIMUM_RESERVE'],unresolved_count=1)
        self.runtime.refresh=Mock(return_value=snapshot)
        for _ in range(5):self.coordinator.refresh_account()
        self.assertEqual(self.coordinator.session_reset_failures,0)
        self.assertFalse(self.probe.restart_requested())

    def test_unknown_dispatch_claim_survives_rebuilt_coordinator(self):
        self.coordinator.bridge.now=lambda:self.fixture.now
        self.runtime.fail_send=True
        report=self.coordinator.bridge.step(self.fixture.result,self.fixture.ds,self.fixture.now)
        self.assertEqual(report['reports'][0]['status'],'DISPATCH_UNKNOWN_DO_NOT_RETRY')
        payload=self.runtime.sent[0]
        rebuilt=LiveCoordinator('unused',self.root/'watch',self.fixture.p,threading.Event(),runtime=self.runtime)
        self.addCleanup(rebuilt.close)
        self.assertEqual(rebuilt.bridge.dispatch(payload)['status'],'SKIPPED_DUPLICATE_HANDOFF')
        self.assertEqual(len(self.runtime.sent),1)

    def source(self):
        return dict(checked_at=self.fixture.at,sources={
            'TWSE_NEWS':'https://openapi.twse.com.tw/news',
            'TWSE_ACTIONS':'https://openapi.twse.com.tw/actions'},
            rows={'TWSE_NEWS':[],'TWSE_ACTIONS':[]},errors={})

    def test_rotating_quote_batch_retains_disclosures_and_reuses_frozen_research(self):
        source=self.source();now=self.fixture.now
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return now
        with patch('opportunity_flow.company_events.current_events',return_value=source),\
             patch('opportunity_flow.live.datetime',Clock):
            first=self.coordinator.pulse(self.fixture.ds,dict(candidates=[dict(symbol='1111',packet=True,state='REVIEW_PENDING',rank_score=1)]))
            self.coordinator.last_quote_refresh=0
            second=self.coordinator.pulse(self.fixture.ds,dict(candidates=[dict(symbol='2222',packet=True,state='REVIEW_PENDING',rank_score=1)]))
        self.assertEqual(event_facts(first[2]['1111']),event_facts(second[2]['1111']))
        self.assertEqual(set(second[2]),set(self.fixture.ds['grouped']))
        state=self.root/'research';runner=FakeRunner('WATCH')
        args=dict(quotes={('TWSE','1111'):self.fixture.quote},runner=runner,use_llm=True,
                  account_context={'1111':self.fixture.context})
        run_cycle(self.fixture.ds,self.fixture.at,self.fixture.p,state,events=first[2],**args)
        calls=len(runner.calls)
        run_cycle(self.fixture.ds,self.fixture.at,self.fixture.p,state,events=second[2],**args)
        self.assertGreater(calls,0)
        self.assertEqual(len(runner.calls),calls)

    def test_degraded_disclosure_defers_paid_review_without_erasing_good_cache(self):
        event=deepcopy(self.runtime.disclosures['1111']);state=self.root/'research';runner=FakeRunner('WATCH')
        args=dict(quotes={('TWSE','1111'):self.fixture.quote},runner=runner,use_llm=True,
                  account_context={'1111':self.fixture.context})
        run_cycle(self.fixture.ds,self.fixture.at,self.fixture.p,state,events={'1111':event},**args)
        count=len(runner.calls)
        degraded=run_cycle(self.fixture.ds,self.fixture.at,self.fixture.p,state,
                           events={'1111':dict(status='SOURCE_UNAVAILABLE')},**args)
        self.assertEqual(next(c for c in degraded['candidates'] if c['symbol']=='1111')['state'],'REVIEW_DEFERRED_EVENT_SOURCE')
        self.assertEqual(len(runner.calls),count)
        restored=run_cycle(self.fixture.ds,self.fixture.at,self.fixture.p,state,events={'1111':event},**args)
        self.assertEqual(len(runner.calls),count)
        self.assertEqual(next(c for c in restored['candidates'] if c['symbol']=='1111')['state'],'RESEARCH_WATCH')

    def test_indexed_disclosures_do_not_mix_company_codes_or_hide_source_failure(self):
        source=self.source()
        source['rows']['TWSE_NEWS']=[dict(公司代號='1111',主旨='停止交易',發言日期='20261118')]
        result=for_symbols(source,{'1111':'TWSE','2222':'TWSE'},self.fixture.now,5)
        self.assertEqual(result['1111'],for_symbol(source,'1111','TWSE',self.fixture.now,5))
        self.assertFalse(result['2222']['announcements'])
        source['errors']['TWSE_NEWS']='source unavailable'
        self.assertEqual(for_symbols(source,{'1111':'TWSE'},self.fixture.now,5)['1111']['status'],'SOURCE_UNAVAILABLE')


class GuardianRescueTests(unittest.TestCase):
    def test_startup_requests_known_orphan_handoff_without_releasing_its_broker_lease(self):
        with tempfile.TemporaryDirectory() as tmp,StateLease(tmp,'broker-runtime'):
            save(folder(tmp)/'runtime-17.json',dict(pid=17,run_id='known',at=time.time()))
            self.assertTrue(supervisor.request_existing_flow_handoff(tmp,dict(child_pid=17,run_id='known')))
            self.assertEqual(read(folder(tmp)/'restart_request.json')['run_id'],'known')
            self.assertTrue(intent(tmp))
            with self.assertRaises(RuntimeError):StateLease(tmp,'broker-runtime').acquire()

    def test_startup_never_requests_an_unidentified_or_different_owner_to_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            save(folder(tmp)/'runtime-17.json',dict(pid=17,run_id='different',at=time.time()))
            self.assertFalse(supervisor.request_existing_flow_handoff(tmp,{}))
            self.assertFalse(supervisor.request_existing_flow_handoff(tmp,dict(child_pid=17,run_id='known')))
            self.assertFalse((folder(tmp)/'restart_request.json').exists())
    def test_real_child_restores_guardian_and_hands_off_existing_broker_leases(self):
        with tempfile.TemporaryDirectory(prefix='flow guardian handoff ') as tmp:
            root=Path(tmp);old=root/'old.py';replacement=root/'replacement.py'
            replacement.write_text('''import os,pathlib,sys,threading
from unittest.mock import Mock,patch
from operations import flow_supervisor as s
r=pathlib.Path(os.environ['FLOW_RECOVERY_ROOT'])
code="from pathlib import Path; from opportunity_flow.recovery import set_intent; from operations.flow_supervisor import broker_owner_absent; r=Path("+repr(str(r))+"); assert broker_owner_absent(r); assert (r/'old-stop').exists(); (r/'new-child').write_text('fresh-owner'); set_intent(r,False,'ISOLATED_TEST_COMPLETE')"
stop=threading.Event();deadline=threading.Timer(8,stop.set);deadline.start()
try:
    with patch.object(s,'RuntimeAudit',return_value=Mock()),patch.object(s,'child_command',return_value=[sys.executable,'-c',code]):
        result=s.supervise(r,r/'dataset',r/'state',stop=stop,poll_seconds=.05)
    (r/'replacement-finished').write_text(str(result))
finally:deadline.cancel()
''',encoding='utf-8')
            old.write_text('''import os,pathlib,subprocess,sys,time
from unittest.mock import patch
from opportunity_flow.recovery import RecoveryProbe
from state_lock import StateLease
r=pathlib.Path(os.environ['FLOW_RECOVERY_ROOT']);p=RecoveryProbe(r,'old-run')
def launch(*args,**kwargs):
    code="import runpy;runpy.run_path("+repr(str(r/'replacement.py'))+")"
    child=subprocess.Popen([sys.executable,'-c',code],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return child.pid
with StateLease(r,'dashboard'),StateLease(r,'broker-runtime'),patch('operations.flow_supervisor.launch_detached',side_effect=launch):
    p.ensure_supervision();assert not p.restart_requested();(r/'old-ready').touch()
    end=time.monotonic()+6
    while time.monotonic()<end:
        p._supervision_checked=-float('inf');p.ensure_supervision()
        if p.restart_requested():
            (r/'old-stop').touch();break
        time.sleep(.02)
    else:raise RuntimeError('Guardian disappearance was not repaired')
''',encoding='utf-8')
            env=dict(os.environ,FLOW_RECOVERY_ROOT=str(root),FLOW_RECOVERY_RUN_ID='old-run',
                     FLOW_RECOVERY_DATASET=str(root/'dataset'),FLOW_RECOVERY_STATE=str(root/'state'))
            lease=StateLease(root,'flow-supervisor').acquire()
            code='import runpy;runpy.run_path('+repr(str(old))+')'
            child=subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                end=time.monotonic()+5
                while not (root/'old-ready').exists() and time.monotonic()<end:time.sleep(.02)
                self.assertTrue((root/'old-ready').exists())
                lease.close()  # OS releases this lease when an actual guardian exits.
                out,err=child.communicate(timeout=12)
                self.assertEqual(child.returncode,0,err.decode(errors='replace'))
                end=time.monotonic()+10
                while not (root/'replacement-finished').exists() and time.monotonic()<end:time.sleep(.02)
                self.assertTrue((root/'new-child').exists())
                self.assertEqual((root/'replacement-finished').read_text(),'0')
            finally:
                lease.close()
                if child.poll() is None:child.terminate();child.wait(timeout=5)

    def test_live_guardian_manual_pause_and_shutdown_do_not_start_another_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);probe=RecoveryProbe(root,'run')
            with patch.dict(os.environ,dict(FLOW_RECOVERY_DATASET=str(root/'data'),FLOW_RECOVERY_STATE=str(root/'state'))),\
                 patch.object(supervisor,'launch_detached') as launch:
                with StateLease(root,'flow-supervisor'):probe.ensure_supervision()
                launch.assert_not_called()
                set_intent(root,False,'USER_STOP');probe._supervision_checked=-float('inf');probe.ensure_supervision()
                launch.assert_not_called()
                set_intent(root,True,'USER_START');probe._supervision_checked=-float('inf')
                with patch('opportunity_flow.recovery.windows_shutting_down',return_value=True):probe.ensure_supervision()
                launch.assert_not_called()

    def test_missing_guardian_starts_replacement_and_requests_own_graceful_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);probe=RecoveryProbe(root,'run',pid=17)
            with patch.dict(os.environ,dict(FLOW_RECOVERY_DATASET=str(root/'data'),FLOW_RECOVERY_STATE=str(root/'state'))),\
                 patch.object(supervisor,'launch_detached',return_value=22) as launch:
                probe.ensure_supervision();probe.ensure_supervision()
            launch.assert_called_once()
            self.assertTrue(probe.restart_requested());self.assertTrue(intent(root))
            request=read(folder(root)/'restart_request.json')
            self.assertEqual((request['pid'],request['run_id'],request['reason']),(17,'run','SUPERVISOR_LOST'))

    def test_guardian_log_or_status_disk_failure_cannot_stop_supervision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);target=folder(root);target.mkdir(parents=True)
            (target/'supervisor.jsonl').mkdir();(target/'supervisor_status.json').mkdir()
            child=Mock(pid=17)
            child.wait.side_effect=lambda **kwargs:(set_intent(root,False,'USER_STOP') or 0)
            with patch.object(supervisor,'RuntimeAudit'),patch.object(supervisor.subprocess,'Popen',return_value=child),\
                 patch.object(supervisor.sys,'stderr',io.StringIO()):
                self.assertEqual(supervisor.supervise(root,root,root,poll_seconds=.01),0)
            child.kill.assert_not_called();child.terminate.assert_not_called()


if __name__=='__main__':unittest.main()
