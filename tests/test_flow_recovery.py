"""Recovery preserves manual stops, account leases, fresh evidence and trade gates."""
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

from opportunity_flow.common import save,read
from opportunity_flow.recovery import RecoveryProbe,folder,heartbeat_reason,intent,set_intent
from operations import flow_supervisor as supervisor
from state_lock import StateLease


class RecoveryStateTests(unittest.TestCase):
    def test_only_a_dead_com_owner_can_be_replaced_after_bounded_shutdown(self):
        from execution import broker_runtime
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();old=Mock();old._thread.is_alive.return_value=True
            replacement=Mock()
            with patch.dict(broker_runtime._runtimes,{root:old},clear=True),\
                 patch.object(broker_runtime,'BrokerRuntime',return_value=replacement) as create:
                self.assertIs(broker_runtime.get_broker_runtime(root),old)
                create.assert_not_called()
                old._thread.is_alive.return_value=False
                self.assertIs(broker_runtime.get_broker_runtime(root),replacement)
                create.assert_called_once_with(root)

    def test_manual_pause_survives_a_new_process_and_corrupt_intent_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.assertTrue(intent(root))
            set_intent(root,False,'USER_STOP')
            self.assertFalse(intent(root))
            self.assertFalse(RecoveryProbe(root,'new-run').row.get('desired_running',False))
            for broken in ('not json','[]','{"desired_running":"true"}'):
                (folder(root)/'intent.json').write_text(broken,encoding='utf-8')
                self.assertFalse(intent(root))
            set_intent(root,True,'USER_START')
            self.assertTrue(intent(root))

    def test_old_account_snapshot_does_not_become_verified_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);now=time.time();probe=RecoveryProbe(root,'run-1',clock=lambda:now,pid=17)
            old=datetime.fromtimestamp(now-3600,timezone.utc).isoformat()
            coordinator=Mock(snapshot=dict(checked_at=old,status='OK',inventory_checked=True,errors=[],reconciliation_errors=[]),broker_read_error=None)
            probe.update(loop=True,coordinator=coordinator,force=True)
            self.assertFalse(read(probe.path)['account_verified'])
            coordinator.snapshot['checked_at']=datetime.fromtimestamp(now-1,timezone.utc).isoformat()
            probe.update(loop=True,coordinator=coordinator,force=True)
            self.assertTrue(read(probe.path)['account_verified'])
            coordinator.snapshot['inventory_checked']=False
            probe.update(loop=True,coordinator=coordinator,force=True)
            self.assertFalse(read(probe.path)['account_verified'])

    def test_gui_updates_do_not_hide_a_stalled_watch_loop(self):
        row=dict(pid=17,run_id='new',at=999,loop_at=1,desired_running=True)
        self.assertEqual(heartbeat_reason(row,17,'new',0,1000),'WATCH_LOOP_STALLED')
        row['desired_running']=False
        self.assertIsNone(heartbeat_reason(row,17,'new',0,1000))
        row['at']=1
        self.assertEqual(heartbeat_reason(row,17,'new',0,1000),'PROCESS_HEARTBEAT_STALE')

    def test_recycled_pid_or_wrong_run_does_not_count_as_current_liveness(self):
        row=dict(pid=17,run_id='old',at=1000,desired_running=True)
        self.assertIsNone(heartbeat_reason(row,17,'new',950,1000))
        self.assertEqual(heartbeat_reason(row,17,'new',0,1000),'STARTUP_HEARTBEAT_MISSING')

    def test_gui_heartbeat_without_a_started_watch_loop_cannot_hide_startup_failure(self):
        row=dict(pid=17,run_id='new',at=1000,desired_running=True)
        self.assertIsNone(heartbeat_reason(row,17,'new',950,1000))
        self.assertEqual(heartbeat_reason(row,17,'new',0,1000),'WATCH_LOOP_NOT_STARTED')

    def test_graceful_restart_request_is_scoped_to_exact_process_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            probe=RecoveryProbe(tmp,'new',pid=17)
            for pid,run,expected in ((18,'new',False),(17,'old',False),(17,'new',True)):
                save(folder(tmp)/'restart_request.json',dict(pid=pid,run_id=run))
                self.assertEqual(probe.restart_requested(),expected)

    def test_child_entry_keeps_formal_flow_and_configuration_without_replaying_advice(self):
        command=supervisor.child_command(Path('account'),Path('dataset'),Path('state'),headless=True,model='gpt-6-sol')
        self.assertIn('opportunity_flow',command)
        self.assertIn('live',command)
        self.assertIn('--headless',command)
        self.assertEqual(command[command.index('--model')+1],'gpt-6-sol')
        self.assertNotIn('--read-only',command)
        self.assertFalse(any('advice.json' in v for v in command))


class FlowSupervisorTests(unittest.TestCase):
    def test_unexpected_process_exit_restarts_but_manual_close_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);first=Mock(pid=11);first.wait.return_value=1
            second=Mock(pid=22)
            def closed(*args,**kwargs):
                set_intent(root,False,'USER_WINDOW_CLOSE')
                return 0
            second.wait.side_effect=closed
            stop=threading.Event()
            with patch.object(supervisor,'broker_owner_absent',return_value=True),\
                 patch.object(supervisor.subprocess,'Popen',side_effect=[first,second]) as launch,\
                 patch.object(stop,'wait',return_value=False):
                self.assertEqual(supervisor.supervise(root,root/'dataset',root/'state',stop=stop),0)
            self.assertEqual(launch.call_count,2)
            first.terminate.assert_not_called();second.terminate.assert_not_called()
            self.assertEqual(read(folder(root)/'supervisor_status.json')['state'],'MANUALLY_PAUSED')
            self.assertEqual(launch.call_args.kwargs['env']['TRADING_STATE_ROOT'],str(root))
            self.assertTrue(launch.call_args.kwargs['env']['FLOW_RECOVERY_RUN_ID'])

    def test_duplicate_supervisors_do_not_start_a_second_broker_owner(self):
        with tempfile.TemporaryDirectory() as tmp,StateLease(tmp,'flow-supervisor'),\
             patch.object(supervisor.subprocess,'Popen') as launch:
            self.assertEqual(supervisor.supervise(Path(tmp),Path(tmp),Path(tmp)),0)
            launch.assert_not_called()

    def test_existing_direct_broker_owner_is_not_terminated_or_replaced(self):
        with tempfile.TemporaryDirectory() as tmp,StateLease(tmp,'broker-runtime'):
            stop=threading.Event()
            with patch.object(stop,'wait',side_effect=lambda _:stop.set()),\
                 patch.object(supervisor.subprocess,'Popen') as launch:
                self.assertEqual(supervisor.supervise(Path(tmp),Path(tmp),Path(tmp),stop=stop),0)
            launch.assert_not_called()
            self.assertEqual(read(folder(tmp)/'supervisor_status.json')['state'],'EXISTING_ACCOUNT_OWNER')

    def test_stale_heartbeat_only_requests_graceful_close_and_waits_for_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);child=Mock(pid=33)
            def finished(*args,**kwargs):
                if child.wait.call_count==1:raise subprocess.TimeoutExpired('flow',5)
                set_intent(root,False,'USER_STOP')
                return 0
            child.wait.side_effect=finished
            with patch.object(supervisor,'broker_owner_absent',return_value=True),\
                 patch.object(supervisor,'heartbeat_reason',return_value='PROCESS_HEARTBEAT_STALE'),\
                 patch.object(supervisor.subprocess,'Popen',return_value=child) as launch:
                supervisor.supervise(root,root,root)
            launch.assert_called_once();child.terminate.assert_not_called();child.kill.assert_not_called()
            request=read(folder(root)/'restart_request.json')
            self.assertEqual(request['pid'],33)
            self.assertEqual(request['run_id'],launch.call_args.kwargs['env']['FLOW_RECOVERY_RUN_ID'])

    def test_restart_backoff_survives_the_supervisor_itself_restarting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);save(folder(root)/'restart_history.json',[time.time()-1]*3)
            stop=threading.Event()
            with patch.object(supervisor,'broker_owner_absent',return_value=True),\
                 patch.object(stop,'wait',side_effect=lambda _:stop.set()),\
                 patch.object(supervisor.subprocess,'Popen') as launch:
                supervisor.supervise(root,root,root,stop=stop)
            launch.assert_not_called()
            self.assertEqual(read(folder(root)/'supervisor_status.json')['state'],'RESTART_BACKOFF')

    def test_manual_live_launcher_detaches_supervisor_from_console(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(supervisor.subprocess,'Popen',return_value=Mock(pid=44)) as launch:
            self.assertEqual(supervisor.launch_detached(Path(tmp),Path(tmp)/'dataset',Path(tmp)/'state'),44)
            args=launch.call_args.args[0]
            self.assertIn('operations.flow_supervisor',args)
            self.assertNotIn('--install',args)
            self.assertNotIn('--resume',args)

    def test_headless_launcher_retains_ctrl_c_and_persists_manual_pause(self):
        import signal
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def run(*args,**kwargs):
                signal.getsignal(signal.SIGINT)(signal.SIGINT,None)
                self.assertTrue(kwargs['stop'].is_set())
                self.assertFalse(intent(root))
                return 0
            previous=signal.getsignal(signal.SIGINT)
            with patch('opportunity_flow.live.resolve_account_root',return_value=root),\
                 patch.object(supervisor.subprocess,'run') as install,\
                 patch.object(supervisor,'launch_detached') as detached,\
                 patch.object(supervisor,'supervise',side_effect=run):
                self.assertEqual(supervisor.main(['--install','--resume','--headless',
                    '--dataset',str(root/'dataset'),'--state',str(root/'state')]),0)
            install.assert_called_once();detached.assert_not_called()
            self.assertIs(signal.getsignal(signal.SIGINT),previous)


@unittest.skipUnless(os.name=='nt','Windows Task Scheduler definition')
class WindowsRecoveryPlanTests(unittest.TestCase):
    def test_installer_plan_has_boot_owner_logon_and_indefinite_liveness_trigger(self):
        import xml.etree.ElementTree as ET
        with tempfile.TemporaryDirectory(prefix='flow recovery ') as tmp:
            root=Path(tmp)
            for name in ('.env','config/execution_live.json','data/execution/live_execution.sqlite3','data/analysis/market_history.sqlite3'):
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.touch()
            script=Path(__file__).resolve().parents[1]/'operations/InstallOpportunityFlowRecovery.ps1'
            result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),
                '-StateRoot',str(root),'-Dataset',str(root/'dataset'),'-State',str(root/'state'),
                '-PythonPath',sys.executable,'-Model','gpt-6-sol','-ValidateOnly'],capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
            plan=json.loads(result.stdout.decode('utf-8-sig'))
            self.assertEqual(plan['trigger_count'],3)
            self.assertTrue(plan['start_when_available'])
            self.assertFalse(plan['credentials_saved'])
            tree=ET.fromstring(plan['xml']);ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
            for tag in ('BootTrigger','LogonTrigger','TimeTrigger'):
                self.assertIsNotNone(tree.find('t:Triggers/t:'+tag,ns))
            self.assertEqual(tree.find('t:Triggers/t:TimeTrigger/t:Repetition/t:Interval',ns).text,'PT1M')
            self.assertEqual(tree.find('t:Principals/t:Principal/t:LogonType',ns).text,'InteractiveToken')
            self.assertEqual(tree.find('t:Settings/t:AllowHardTerminate',ns).text,'false')
            arguments=tree.find('t:Actions/t:Exec/t:Arguments',ns).text
            self.assertIn('--model "gpt-6-sol"',arguments)
            self.assertNotIn('--resume',arguments)
            self.assertNotIn('--install',arguments)
            self.assertFalse((folder(root)/'task-registration.json').exists())


if __name__=='__main__':
    unittest.main()
