"""Actual November 20 failures: false version mismatch, duplicate publishers and missing veto reasons."""
from copy import deepcopy
from datetime import datetime,timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from opportunity_flow.common import read,save
from opportunity_flow.recovery import code_revision,folder,intent,runtime_fingerprint,set_intent,source_fingerprint
from opportunity_flow.legacy_runtime import LegacyRuntime
from opportunity_flow.state import Ledger
from operations.flow_runtime_audit import assess
from operations import flow_supervisor as supervisor
from operations.flow_startup_handoff import pending_token,prepare_handoff,wait_for_handoff
from state_lock import StateLease


class EvidenceConsistencyTests(unittest.TestCase):
    def test_report_only_changes_and_line_endings_do_not_change_runtime_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);subprocess.run(['git','init','-q',str(root)],check=True,env=dict(os.environ))
            (root/'engine.py').write_bytes(b'limit=10000\n')
            (root/'prompt.md').write_text('Review all risks.\n',encoding='utf-8')
            (root/'reports').mkdir();save(root/'reports/snapshot.json',dict(state='before'))
            subprocess.run(['git','add','--','engine.py','prompt.md','reports/snapshot.json'],cwd=root,check=True,capture_output=True,env=dict(os.environ))
            original=source_fingerprint(root);self.assertTrue(original)
            save(root/'reports/snapshot.json',dict(state='after'))
            (root/'engine.py').write_bytes(b'limit=10000\r\n')
            self.assertEqual(source_fingerprint(root),original)
            (root/'prompt.md').write_text('Review new rules.\n',encoding='utf-8')
            self.assertNotEqual(source_fingerprint(root),original)
            (root/'prompt.md').write_text('Review all risks.\n',encoding='utf-8')
            (root/'engine.py').write_bytes(b'limit=5000\n')
            self.assertNotEqual(source_fingerprint(root),original)

    def assessment(self,revision,child_revision,parent_fp,child_fp):
        row=dict(pid=17,run_id='current',at=999,loop_at=999,controller_status='RUNNING',
            code_revision=child_revision,runtime_fingerprint=child_fp,desired_running=True,
            account_verified=True,account_checked_at=datetime.fromtimestamp(999,timezone.utc).isoformat(),
            executor_enabled=True,read_only=False,stages={
                'ACCOUNT_RECONCILED':dict(first_at=950,last_at=950,count=1),
                'RESEARCH_CYCLE_COMPLETED':dict(first_at=960,last_at=960,count=1)})
        native=dict(startup_verified=True,current_session_is_interactive=True,
                    inspected_at=datetime.fromtimestamp(999,timezone.utc).isoformat())
        return assess(native,row,pid=17,run_id='current',launched_at=900,now=1000,
            process_alive=True,revision=revision,fingerprint=parent_fp,process_state='RUNNING')

    def test_matching_code_with_different_report_commits_passes_but_changed_code_does_not(self):
        self.assertEqual(self.assessment('a'*40,'b'*40,'c'*64,'c'*64)['state'],'AUTOMATION_ACTIVE')
        self.assertEqual(self.assessment('a'*40,'a'*40,'c'*64,'d'*64)['state'],'RUNNING_VERSION_UNVERIFIED')
        self.assertEqual(self.assessment('a'*40,'b'*40,None,None)['state'],'RUNNING_VERSION_UNVERIFIED')

    def test_duplicate_periodic_start_never_overwrites_the_live_owner_report(self):
        with tempfile.TemporaryDirectory() as tmp,StateLease(tmp,'flow-supervisor'):
            value=dict(state='AUTOMATION_ACTIVE',source_revision='old-report-commit')
            save(folder(tmp)/'runtime-audit.json',value)
            with patch.object(supervisor,'RuntimeAudit') as audit,patch.object(supervisor.subprocess,'Popen') as child:
                self.assertEqual(supervisor.supervise(Path(tmp),Path(tmp),Path(tmp)),0)
            self.assertEqual(read(folder(tmp)/'runtime-audit.json'),value)
            audit.assert_not_called();child.assert_not_called()

    def heartbeat_read_case(self,age):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);clock=[0.];child=Mock(pid=17);attempts=[0];original_read=supervisor.read
            def wait(**kwargs):
                if child.wait.call_count<=2:
                    clock[0]=1000 if child.wait.call_count==1 else 1000+age
                    raise subprocess.TimeoutExpired('isolated',5)
                set_intent(root,False,'USER_STOP');return 0
            child.wait.side_effect=wait
            def read_file(path):
                if Path(path).name=='runtime-17.json':
                    attempts[0]+=1
                    if attempts[0]==2:raise PermissionError('isolated transient read contention')
                    return dict(pid=17,run_id=launch.call_args.kwargs['env']['FLOW_RECOVERY_RUN_ID'],
                        at=1000,loop_at=1000,desired_running=True)
                return original_read(path)
            with patch.object(supervisor,'RuntimeAudit'),patch.object(supervisor,'broker_owner_absent',return_value=True),\
                 patch.object(supervisor.subprocess,'Popen',return_value=child) as launch,\
                 patch.object(supervisor,'read',side_effect=read_file),patch.object(supervisor.time,'time',side_effect=lambda:clock[0]):
                supervisor.supervise(root,root,root,poll_seconds=.01)
            path=folder(root)/'restart_request.json'
            request=read(path) if path.exists() else None
            child.terminate.assert_not_called();child.kill.assert_not_called()
            return request

    def test_one_unreadable_heartbeat_during_long_run_does_not_trigger_false_startup_restart(self):
        self.assertIsNone(self.heartbeat_read_case(5))

    def test_sustained_missing_heartbeat_still_requests_graceful_recovery(self):
        self.assertEqual(self.heartbeat_read_case(200)['reason'],'PROCESS_HEARTBEAT_STALE')


class StartupHandoffTests(unittest.TestCase):
    def test_real_process_upgrade_releases_all_old_leases_before_resuming(self):
        with tempfile.TemporaryDirectory(prefix='flow explicit upgrade ') as tmp:
            root=Path(tmp)
            code='''import os,sys,time
from pathlib import Path
from opportunity_flow.common import read,save
from opportunity_flow.recovery import folder
from state_lock import StateLease
r=Path(sys.argv[1]);pid=os.getpid()
with StateLease(r,'flow-supervisor'),StateLease(r,'dashboard'),StateLease(r,'broker-runtime'):
    save(folder(r)/'supervisor_status.json',dict(child_pid=pid,run_id='isolated-old'))
    save(folder(r)/f'runtime-{pid}.json',dict(pid=pid,run_id='isolated-old',at=time.time(),code_revision='0'*40,runtime_fingerprint='0'*64))
    (r/'ready').touch();end=time.monotonic()+8
    while time.monotonic()<end:
        try:request=read(folder(r)/'restart_request.json')
        except (OSError,ValueError):request={}
        if request.get('pid')==pid and request.get('run_id')=='isolated-old':break
        time.sleep(.01)
    else:raise RuntimeError('No scoped graceful upgrade request')
    (r/'graceful-stop').touch()
'''
            child=subprocess.Popen([sys.executable,'-c',code,str(root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                end=time.monotonic()+5
                while not (root/'ready').exists() and time.monotonic()<end:time.sleep(.01)
                self.assertTrue((root/'ready').exists())
                token=prepare_handoff(root);self.assertTrue(token);self.assertFalse(intent(root))
                stop=threading.Event();timer=threading.Timer(8,stop.set);timer.start()
                try:self.assertTrue(wait_for_handoff(root,token,stop=stop,pause=lambda _:time.sleep(.01)))
                finally:timer.cancel()
                out,err=child.communicate(timeout=3)
                self.assertEqual(child.returncode,0,err.decode(errors='replace'))
                self.assertTrue((root/'graceful-stop').exists());self.assertTrue(intent(root))
                with StateLease(root,'flow-supervisor'),StateLease(root,'dashboard'),StateLease(root,'broker-runtime'):pass
            finally:
                if child.poll() is None:child.terminate();child.communicate(timeout=3)

    def test_original_live_launcher_carries_upgrade_token_without_early_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);set_intent(root,False,'USER_STOP')
            with patch('opportunity_flow.live.resolve_account_root',return_value=root),\
                 patch.object(supervisor.subprocess,'run'),\
                 patch.object(supervisor,'prepare_handoff',return_value='a'*32),\
                 patch.object(supervisor,'launch_detached',return_value=44) as launch,\
                 patch.object(supervisor,'wait_for_startup'):
                self.assertEqual(supervisor.main(['--install','--resume','--dataset',str(root),'--state',str(root)]),0)
            self.assertEqual(launch.call_args.kwargs['handoff_token'],'a'*32)
            self.assertFalse(intent(root))

    def prepare(self,root):
        save(folder(root)/'supervisor_status.json',dict(child_pid=17,run_id='old'))
        save(folder(root)/'runtime-17.json',dict(pid=17,run_id='old',at=time.time(),
            code_revision='0'*40,runtime_fingerprint='0'*64))
        return prepare_handoff(root)

    def test_explicit_upgrade_waits_for_all_old_owner_leases_then_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard=StateLease(tmp,'flow-supervisor').acquire();broker=StateLease(tmp,'broker-runtime').acquire()
            try:
                token=self.prepare(tmp);self.assertTrue(token);self.assertFalse(intent(tmp))
                self.assertEqual(read(folder(tmp)/'restart_request.json')['run_id'],'old')
                waits=[]
                def pause(seconds):
                    waits.append(seconds)
                    if len(waits)==1:guard.close()
                    else:broker.close()
                self.assertTrue(wait_for_handoff(tmp,token,pause=pause))
                self.assertEqual(len(waits),2);self.assertTrue(intent(tmp))
            finally:guard.close();broker.close()

    def test_manual_stop_during_upgrade_cancels_automatic_resume(self):
        with tempfile.TemporaryDirectory() as tmp,StateLease(tmp,'flow-supervisor'):
            token=self.prepare(tmp)
            def pause(seconds):set_intent(tmp,False,'USER_STOP')
            self.assertFalse(wait_for_handoff(tmp,token,pause=pause))
            self.assertFalse(intent(tmp));self.assertIsNone(pending_token(tmp))

    def test_same_code_owner_is_left_running_across_report_commit_changes(self):
        with tempfile.TemporaryDirectory() as tmp,StateLease(tmp,'flow-supervisor'):
            save(folder(tmp)/'supervisor_status.json',dict(child_pid=17,run_id='current'))
            save(folder(tmp)/'runtime-17.json',dict(pid=17,run_id='current',at=time.time(),
                code_revision='f'*40,runtime_fingerprint=runtime_fingerprint()))
            self.assertIsNone(prepare_handoff(tmp));self.assertTrue(intent(tmp))
            self.assertFalse((folder(tmp)/'restart_request.json').exists())


class PretradeEvidenceTests(unittest.TestCase):
    def runtime(self,root):
        runtime=LegacyRuntime.__new__(LegacyRuntime)
        runtime.output=root/'broker';runtime.model='test';runtime.stop_event=None
        runtime.config=SimpleNamespace(economic_minimum_commission_twd=Decimal(1))
        runtime.snapshot=dict(strategy_cap_twd=10000,current_buy_budget_twd=4080,budget_status='READY')
        return runtime

    def test_veto_reason_and_validator_outcome_are_persisted_without_account_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);runtime=self.runtime(root)
            runtime.provider=Mock(return_value=(json.dumps(dict(reviews=[dict(symbol='8089',approved=False,reason='Fees or data need review')])),{}))
            decisions=[dict(symbol='8089',decision='BUY',reason='frozen plan',warnings=[],
                suggested_qty=100,suggested_price=33,target_price=36,invalidation_price=32)]
            final,review=runtime.pretrade(decisions,[dict(symbol='8089',private_account='PRIVATE_ACCOUNT')])
            self.assertEqual(final[0]['decision'],'WAIT');self.assertEqual(review['status'],'VALID')
            ledger=Ledger(root)
            try:rows=ledger.db.execute("SELECT data FROM feed_events WHERE kind='PRETRADE_REVIEW_RESULT'").fetchall()
            finally:ledger.close()
            saved=json.loads(rows[0][0]);self.assertFalse(saved['reviews'][0]['approved'])
            self.assertEqual(saved['reviews'][0]['reason'],'Fees or data need review')
            self.assertNotIn('PRIVATE_ACCOUNT',str(saved));runtime.provider.assert_called_once()

    def test_model_call_records_response_but_transport_valid_does_not_imply_approval(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(os,'environ',{**os.environ,'CAPITAL_USER_ID':'SECRET_ACCOUNT'}):
            root=Path(tmp);runtime=self.runtime(root)
            response=SimpleNamespace(output_text=json.dumps(dict(reviews=[dict(symbol='8089',approved=False,reason='SECRET_ACCOUNT veto')],
                ignored_private='PRIVATE_REQUEST')),usage=SimpleNamespace(model_dump=lambda:dict(input_tokens=2,output_tokens=1)))
            client=Mock();client.responses.create.return_value=response
            with patch('codex_cli.CodexClient',return_value=client):
                runtime.provider(dict(input='PRIVATE_ACCOUNT_CONTEXT',text=dict(format=dict(name='pretrade_review'))))
            ledger=Ledger(root)
            try:value=json.loads(ledger.db.execute('SELECT data FROM calls').fetchone()[0])
            finally:ledger.close()
            self.assertEqual(value['validation_phase'],'TRANSPORT_ONLY')
            self.assertFalse(value['answer']['reviews'][0]['approved'])
            self.assertNotIn('PRIVATE_',str(value));self.assertNotIn('SECRET_ACCOUNT',str(value))


if __name__=='__main__':unittest.main()
