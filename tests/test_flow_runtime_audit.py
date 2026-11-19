"""Live acceptance cannot be inferred from old snapshots or synthetic success flags."""
from copy import deepcopy
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from uuid import uuid4

from opportunity_flow.common import read,save
from opportunity_flow.recovery import RecoveryProbe,folder,observe,observe_execution
from operations.flow_runtime_audit import RuntimeAudit,assess,wait_for_startup
from operations import flow_supervisor as supervisor


class RuntimeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.now=1000.
        self.revision='a'*40
        self.native=dict(inspected_at=datetime.fromtimestamp(999,timezone.utc).isoformat(),
            startup_verified=True,current_session_is_interactive=True,shortcut_verified=True,
            task_definition_verified=False,effective_triggers=['OWNER_LOGON'],credentials_saved=False)
        self.row=dict(pid=17,run_id='current',code_revision=self.revision,at=999,loop_at=998,
            controller_status='RUNNING',account_verified=True,account_checked_at=datetime.fromtimestamp(999,timezone.utc).isoformat(),
            read_only=False,executor_enabled=True,budget_ready=True,desired_running=True,stages={},events=[])
        for stage,at in [('ACCOUNT_RECONCILED',950),('RESEARCH_CYCLE_COMPLETED',960)]:
            self.row['stages'][stage]=dict(first_at=at,last_at=at,count=1,detail={})

    def assess(self,**kwargs):
        return assess(self.native,self.row,pid=17,run_id='current',launched_at=900,now=self.now,
            process_alive=True,revision=self.revision,process_state='RUNNING',**kwargs)

    def test_active_flow_does_not_invent_order_or_fill_evidence(self):
        report=self.assess()
        self.assertEqual(report['state'],'AUTOMATION_ACTIVE')
        self.assertEqual(report['order_evidence'],dict(sent_events=0,fill_events=0,unknown_events=0))
        self.assertFalse(report['position_management_observed'])
        self.assertEqual(report['windows']['effective_triggers'],['OWNER_LOGON'])

    def test_existing_snapshot_before_this_launch_cannot_pass_even_if_marked_verified(self):
        self.row['account_checked_at']=datetime.fromtimestamp(899,timezone.utc).isoformat()
        self.assertEqual(self.assess()['state'],'AWAITING_FRESH_ACCOUNT')
        self.row['account_checked_at']=datetime.fromtimestamp(999,timezone.utc).isoformat()
        self.row['stages'].pop('ACCOUNT_RECONCILED')
        self.assertFalse(self.assess()['checks']['fresh_account_reconciled'])

    def test_wrong_run_wrong_version_stale_loop_and_read_only_cannot_pass(self):
        for key,value in [('run_id','previous'),('code_revision','b'*40),('at',1),('loop_at',1),
                          ('read_only',True),('executor_enabled',False),('broker_read_blocked',True)]:
            with self.subTest(key=key):
                old=deepcopy(self.row);self.row[key]=value
                self.assertNotEqual(self.assess()['state'],'AUTOMATION_ACTIVE')
                self.row=old

    def test_live_task_failure_and_stale_os_inspection_are_reported(self):
        self.native['startup_verified']=False
        self.assertEqual(self.assess()['state'],'STARTUP_CONFIGURATION_BLOCKED')
        self.native['startup_verified']=True
        self.native['inspected_at']=datetime.fromtimestamp(1,timezone.utc).isoformat()
        self.assertFalse(self.assess()['checks']['startup_verified'])

    def test_later_data_wait_or_research_failure_is_not_hidden_by_an_earlier_cycle(self):
        self.row['stages']['WAITING_DAILY_DATA']=dict(first_at=980,last_at=980,count=1,detail={})
        self.assertEqual(self.assess()['state'],'WAITING_DAILY_DATA')
        self.row['stages']['RESEARCH_ERROR']=dict(first_at=990,last_at=990,count=1,detail={})
        self.assertEqual(self.assess()['state'],'RESEARCH_BLOCKED')

    def test_outside_hours_wait_is_distinct_from_research_or_orders(self):
        self.row['stages'].pop('RESEARCH_CYCLE_COMPLETED')
        self.row['stages']['WAITING_RESEARCH_WINDOW']=dict(first_at=990,last_at=999,count=1,detail={})
        report=self.assess()
        self.assertEqual(report['state'],'WAITING_RESEARCH_WINDOW')
        self.assertTrue(report['checks']['automatic_execution_enabled'])
        self.assertFalse(report['checks']['research_cycle_observed'])

    def test_previous_session_events_and_unallowlisted_private_fields_are_not_exported(self):
        self.row['stages']['FILL_CONFIRMED']=dict(first_at=899,last_at=999,count=4,detail={})
        self.row['account_identity']='PRIVATE_ACCOUNT_SENTINEL'
        self.row['events']=[dict(at=999,stage='RESEARCH_CYCLE_COMPLETED',password='PRIVATE_SECRET_SENTINEL')]
        self.native['arguments']='PRIVATE_PATH_SENTINEL'
        report=self.assess();raw=json.dumps(report)
        self.assertEqual(report['order_evidence']['fill_events'],0)
        self.assertNotIn('PRIVATE_',raw)

    def test_export_updates_private_and_git_shareable_reports_without_broker_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);audit=RuntimeAudit(root/'account',root/'dataset',root/'state',code_root=root,clock=lambda:1000)
            audit.native=self.native;audit.native_at=1000;audit.revision=self.revision
            report=audit.publish('RUNNING',row=self.row,pid=17,run_id='current',launched_at=900,process_alive=True)
            self.assertEqual(report['state'],'AUTOMATION_ACTIVE')
            self.assertEqual(read(root/'reports/opportunity_flow_runtime/latest.json'),report)
            self.assertTrue((root/'reports/opportunity_flow_runtime/latest.txt').exists())
            self.assertEqual(len(list((root/'reports/opportunity_flow_runtime/runs').glob('*.json'))),1)
            self.assertEqual(read(folder(root/'account')/'runtime-audit.json'),report)

    def test_unwritable_git_export_keeps_account_copy_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'reports').write_text('occupied')
            audit=RuntimeAudit(root/'account',root/'dataset',root/'state',code_root=root,clock=lambda:1000)
            audit.native=self.native;audit.native_at=1000;audit.revision=self.revision
            result=audit.publish('RUNNING',row=self.row,pid=17,run_id='current',launched_at=900,process_alive=True)
            self.assertEqual(result['export_error'],'REPOSITORY_EXPORT_UNAVAILABLE')
            self.assertEqual(read(folder(root/'account')/'runtime-audit.json')['export_error'],result['export_error'])

    def test_stage_write_failure_never_changes_executor_outcome_or_triggers_retry(self):
        probe=Mock();probe.event.side_effect=OSError('disk unavailable')
        with patch('opportunity_flow.recovery.get_probe',return_value=probe):
            observe('ORDER_SENT',symbol='1111')
        probe.event.assert_called_once()

    def test_uncertain_dispatch_is_exported_as_unknown_and_never_invented_as_zero_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            probe=RecoveryProbe(tmp,'current',clock=lambda:999,pid=17)
            with patch('opportunity_flow.recovery.get_probe',return_value=probe):
                observe_execution(dict(status='DISPATCH_UNKNOWN_DO_NOT_RETRY',error='PRIVATE_REPLY'))
                observe_execution(dict(results=[dict(status='UNCONFIRMED',broker_order_sent=None,
                                                      symbol='1111',side='BUY',reason='PRIVATE_REPLY')]))
            self.row['stages']=probe.row['stages'];self.row['events']=probe.row['events']
            report=self.assess()
            self.assertEqual(report['order_evidence']['unknown_events'],2)
            self.assertEqual(report['order_evidence']['fill_events'],0)
            self.assertNotIn('PRIVATE_REPLY',json.dumps(report))

    def test_startup_wait_ignores_old_report_and_waits_for_fresh_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock=[1000.];root=Path(tmp)
            value=dict(exported_at=datetime.fromtimestamp(900,timezone.utc).isoformat(),source_revision=self.revision,
                       state='AUTOMATION_ACTIVE',checks={})
            save(folder(root)/'runtime-audit.json',value)
            def pause(seconds):
                clock[0]+=seconds
                value['exported_at']=datetime.fromtimestamp(clock[0],timezone.utc).isoformat()
                save(folder(root)/'runtime-audit.json',value)
            with patch('operations.flow_runtime_audit.code_revision',return_value=self.revision),patch('builtins.print'):
                result=wait_for_startup(root,timeout=5,not_before=1000,clock=lambda:clock[0],pause=pause)
            self.assertEqual(clock[0],1002)
            self.assertEqual(result['state'],'AUTOMATION_ACTIVE')

    def test_gui_launcher_installs_resumes_and_waits_for_live_report(self):
        with tempfile.TemporaryDirectory() as tmp,patch('opportunity_flow.live.resolve_account_root',return_value=Path(tmp)),\
             patch.object(supervisor.subprocess,'run') as install,\
             patch.object(supervisor,'launch_detached',return_value=44) as launch,\
             patch.object(supervisor,'wait_for_startup') as verify,patch('builtins.print'):
            self.assertEqual(supervisor.main(['--install','--resume','--dataset',tmp,'--state',tmp]),0)
            install.assert_called_once();launch.assert_called_once();verify.assert_called_once()
            self.assertTrue(read(folder(tmp)/'intent.json')['desired_running'])

    def test_supervised_headless_reconciles_before_waiting_for_daily_data(self):
        import test_flow_execution_bridge as bridge_fixture
        from opportunity_flow.live import LiveCoordinator
        from opportunity_flow.__main__ import main
        fixture=bridge_fixture.BridgeTests('test_approved_research_reaches_existing_interface_without_chasing')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        stop=threading.Event();root=fixture.root/'startup';root.mkdir()
        runtime=fixture.runtime;runtime.config.enabled=True
        runtime.snapshot['checked_at']=datetime.now(timezone.utc).isoformat()
        coordinator=LiveCoordinator('unused',root/'state',fixture.p,stop,runtime=runtime)
        probe=RecoveryProbe(root,'headless-startup')
        def collect(*args,**kwargs):
            self.assertTrue(coordinator.snapshot['inventory_checked'])
            self.assertIn('ACCOUNT_RECONCILED',probe.row['stages'])
            stop.set()
            return dict(calendar_verified_through='2026-11-17',http_requests=0,cache_hits=0)
        with patch('opportunity_flow.recovery.get_probe',return_value=probe),\
             patch('opportunity_flow.__main__.refresh_due',return_value='2026-11-17'),\
             patch('opportunity_flow.collector.collect',side_effect=collect) as download,patch('builtins.print'):
            main(['watch','--dataset',str(root/'missing-data'),'--state',str(root/'state'),
                  '--live','--llm','--refresh-daily','--poll-seconds','1'],
                  coordinator=coordinator,external_stop=stop)
        download.assert_called_once()
        self.assertFalse(runtime.sent)
        self.assertIn('WAITING_DAILY_DATA',probe.row['stages'])

    def test_real_child_heartbeat_reaches_automatic_export_then_manual_pause(self):
        # Real processes and file protocol, with an isolated synthetic account.
        with tempfile.TemporaryDirectory(prefix='flow child audit ') as tmp:
            root=Path(tmp);child=root/'child.py'
            child.write_text('''from datetime import datetime,timezone
from types import SimpleNamespace
import time
from opportunity_flow.common import read
from opportunity_flow.recovery import folder,get_probe,set_intent
p=get_probe()
c=SimpleNamespace(snapshot=dict(checked_at=datetime.now(timezone.utc).isoformat(),inventory_checked=True,
    status='OK',errors=[],reconciliation_errors=[],budget_status='READY',current_buy_budget_twd='10000',strategy_cap_twd='10000'),
    broker_read_error=None,runtime=SimpleNamespace(read_only=False,config=SimpleNamespace(enabled=True,
        min_available_to_buy_twd=5000,max_daily_buy_twd=30000)))
p.event('ACCOUNT_RECONCILED');p.event('WATCH_STARTED');p.event('RESEARCH_CYCLE_COMPLETED',count=1)
p.update(loop=True,coordinator=c,controller_status='RUNNING',force=True)
for _ in range(100):
    try:
        if read(folder(p.root)/'runtime-audit.json').get('state')=='AUTOMATION_ACTIVE':break
    except (OSError,ValueError):pass
    time.sleep(.05)
else:raise RuntimeError('Parent did not export current child acceptance')
set_intent(p.root,False,'ISOLATED_CHILD_TEST_STOP')
''',encoding='utf-8')
            # Script imports use the real repository, never a cloned account.
            command=[sys.executable,'-c',"import runpy; runpy.run_path("+repr(str(child))+")"]
            audit=RuntimeAudit(root,root/'dataset',root/'state',code_root=root)
            observed=[];publish=audit.publish
            def capture(*args,**kwargs):
                value=publish(*args,**kwargs);observed.append(value['state']);return value
            audit.publish=capture
            native={**self.native, 'inspected_at':datetime.now(timezone.utc).isoformat()}
            stop=threading.Event();deadline=threading.Timer(12,stop.set)
            with patch('operations.flow_runtime_audit.native_inspection',return_value=native),\
                 patch.object(supervisor,'RuntimeAudit',return_value=audit),\
                 patch.object(supervisor,'child_command',return_value=command):
                deadline.start()
                try:self.assertEqual(supervisor.supervise(root,root/'dataset',root/'state',poll_seconds=.05,stop=stop),0)
                finally:deadline.cancel()
            self.assertIn('AUTOMATION_ACTIVE',observed)
            final=read(root/'reports/opportunity_flow_runtime/latest.json')
            self.assertEqual(final['state'],'MANUALLY_PAUSED')
            self.assertIn('ACCOUNT_RECONCILED',final['stages'])
            self.assertFalse(final['checks']['automatic_execution_enabled'])
            self.assertEqual(final['order_evidence']['sent_events'],0)


@unittest.skipUnless(os.name=='nt','Windows live configuration inspection')
class NativeRuntimeInspectionTests(unittest.TestCase):
    def invoke(self,root,*args):
        repo=Path(__file__).resolve().parents[1]
        result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',
            str(repo/'operations/InstallOpportunityFlowRecovery.ps1'),'-StateRoot',str(root),
            '-Dataset',str(root/'dataset'),'-State',str(root/'state'),'-PythonPath',sys.executable,*args],
            capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        return json.loads(result.stdout.decode('utf-8-sig'))

    def account(self,root):
        for name in ('.env','config/execution_live.json','data/execution/live_execution.sqlite3','data/analysis/market_history.sqlite3'):
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.touch()

    def test_read_only_inspection_of_missing_objects_cannot_claim_startup_installed(self):
        with tempfile.TemporaryDirectory(prefix='flow inspect ') as tmp:
            root=Path(tmp);self.account(root)
            value=self.invoke(root,'-TaskName','STOCKLLM Isolated Check-'+uuid4().hex,'-InspectOnly')
            self.assertFalse(value['startup_verified'])
            self.assertFalse(value['task_present'])
            self.assertFalse(value['shortcut_verified'])
            self.assertFalse(folder(root).exists())
            self.assertTrue(value['boot_at'])
            self.assertNotIn(str(root),json.dumps(value))

    def test_actual_registration_then_os_inspection_and_cleanup_of_isolated_startup(self):
        with tempfile.TemporaryDirectory(prefix='flow startup verification ') as tmp:
            root=Path(tmp);self.account(root)
            task='STOCKLLM Isolated Check-'+uuid4().hex
            installed={}
            cleanup=root/'cleanup.ps1'
            cleanup.write_text('''param($TaskName,$AccountRoot,$StartupLink)
$ErrorActionPreference='Stop'
if (-not $TaskName.StartsWith('STOCKLLM Isolated Check-')) { throw 'Wrong task scope' }
$s=New-Object -ComObject Schedule.Service; $s.Connect(); $f=$s.GetFolder('\\')
if ($StartupLink) {
    $link=[IO.Path]::GetFullPath($StartupLink)
    if ((Split-Path -Parent $link) -ne [IO.Path]::GetFullPath([Environment]::GetFolderPath('Startup'))) { throw 'Outside Startup' }
    $w=New-Object -ComObject WScript.Shell; $shortcut=$w.CreateShortcut($link)
    if (-not $shortcut.Arguments.Contains($AccountRoot)) { throw 'Wrong account scope' }
    Remove-Item -LiteralPath $link
} else {
    try { $t=$f.GetTask($TaskName) }
    catch {
        $failure=$_.Exception
        while ($failure.InnerException) { $failure=$failure.InnerException }
        if ($failure.HResult -eq -2147024894) { exit 0 }
        throw
    }
    if (-not $t.Definition.Actions.Item(1).Arguments.Contains($AccountRoot)) { throw 'Wrong task account scope' }
    $f.DeleteTask($TaskName,0)
}
''',encoding='utf-8')
            try:
                installed=self.invoke(root,'-TaskName',task,'-RegisterOnly')
                result=self.invoke(root,'-TaskName',task,'-InspectOnly')
                self.assertTrue(result['startup_verified'],result)
                expected=installed['effective_triggers']
                self.assertEqual(result['effective_triggers'],expected)
                self.assertFalse(result['credentials_saved'])
                self.assertFalse(result['automatic_force_kill'])
                if not installed['task_registered']:
                    # An old receipt must not hide a missing real shortcut.
                    # The finally block performs the scoped removal first.
                    self.assertTrue(result['shortcut_verified'])
            finally:
                result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(cleanup),
                    '-TaskName',task,'-AccountRoot',str(root),'-StartupLink',installed.get('startup_shortcut','')],
                    capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
            after=self.invoke(root,'-TaskName',task,'-InspectOnly')
            self.assertFalse(after['startup_verified'])


if __name__=='__main__':unittest.main()
