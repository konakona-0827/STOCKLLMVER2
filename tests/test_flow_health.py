"""UI refresh and diagnostic checks must not start or mutate trading actions."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime,timedelta
from pathlib import Path
import sqlite3
import tempfile
from threading import Event,Thread
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from opportunity_flow.activity import ActivityLog
from opportunity_flow.common import TAIPEI,save
from opportunity_flow.dashboard_service import FlowController,FlowViewService
from opportunity_flow.health import inspect_health


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.state=self.root/'flow';self.dataset=self.root/'dataset';self.at=datetime(2026,9,30,14,0,tzinfo=TAIPEI)
        for name in ('data/execution/live_execution.sqlite3','data/analysis/market_history.sqlite3','flow/flow.sqlite3'):
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(path)) as db:db.execute('CREATE TABLE fixture (id INTEGER)');db.commit()
        save(self.root/'config/execution_live.json',dict(enabled=True))
        save(self.dataset/'manifest.json',dict(calendar_verified_through='2026-09-29'))
        self.snapshot=dict(checked_at=self.at.isoformat(),status='OK',errors=[],broker_inventory_verified=True,
            inventory_comparison=[dict(symbol='1111',broker_cash_qty=10,status='MATCH')],budget_status='READY',
            current_buy_budget_twd='1000',buy_block_reasons=[],quote_check_status='OUTSIDE_TRADING_SESSION')
        self.args=SimpleNamespace(state=str(self.state),dataset=str(self.dataset),read_only=False)

    def inspect(self,snapshot=None):
        with patch('opportunity_flow.data.load_dataset',return_value=dict(grouped={'1111':[]},sessions=['2026-09-29'])):
            return inspect_health(self.root,self.state,self.dataset,snapshot or self.snapshot,
                controller_status='RUNNING',at=self.at)

    def test_healthy_read_only_inspection_leaves_database_bytes_unchanged(self):
        files=list(self.root.rglob('*.sqlite3'));before={p:p.read_bytes() for p in files}
        report=self.inspect()
        self.assertEqual(report['status'],'PASS',report)
        self.assertEqual(report['order_submission_calls'],0);self.assertEqual(report['cancellation_calls'],0)
        self.assertTrue(all(p.read_bytes()==before[p] for p in files))

    def test_corrupt_formal_ledger_is_reported_without_repairing_it(self):
        path=self.root/'data/execution/live_execution.sqlite3';path.write_bytes(b'not a SQLite ledger')
        report=self.inspect();checks={r['key']:r for r in report['checks']}
        self.assertEqual(report['status'],'FAIL');self.assertEqual(checks['execution_db']['status'],'FAIL')
        self.assertEqual(checks['broker']['status'],'PASS');self.assertEqual(path.read_bytes(),b'not a SQLite ledger')

    def test_stale_broker_zero_budget_and_unknown_order_remain_separate_findings(self):
        snapshot=deepcopy(self.snapshot);snapshot.update(checked_at=(self.at-timedelta(minutes=5)).isoformat(),
            current_buy_budget_twd='0',budget_status='BLOCKED',buy_block_reasons=['BELOW_MINIMUM_BALANCE'],
            unresolved_attempts=[dict(decision_id='unknown',status='UNCONFIRMED')])
        report=self.inspect(snapshot);checks={r['key']:r for r in report['checks']}
        self.assertEqual(checks['broker']['status'],'FAIL');self.assertEqual(checks['buy_budget']['status'],'WARN')
        self.assertEqual(checks['orders']['status'],'WARN');self.assertEqual(checks['execution_db']['status'],'PASS')
        self.assertIn('不為測試降低',checks['buy_budget']['remediation'])
        self.assertIn('不重送',checks['orders']['remediation'])

    def test_broker_failure_still_checks_local_ledgers_and_never_starts_the_controller(self):
        controller=SimpleNamespace(status='STOPPED',error='',refresh_account=Mock(side_effect=TimeoutError('BROKER_READ_DEADLINE_EXCEEDED')),start=Mock())
        view=FlowViewService(self.root,self.args,controller)
        with patch('opportunity_flow.data.load_dataset',return_value=dict(grouped={},sessions=[])):
            report=view.check_health()
        checks={r['key']:r for r in report['checks']}
        self.assertEqual(checks['broker']['status'],'FAIL');self.assertEqual(checks['execution_db']['status'],'PASS')
        self.assertEqual(checks['analysis_db']['status'],'PASS');controller.start.assert_not_called()
        self.assertTrue((self.state/'health/latest.json').exists())

    def test_unchanged_view_is_cached_and_does_not_requery_or_write_sqlite(self):
        view=FlowViewService(self.root,self.args)
        path=self.root/'data/analysis/market_history.sqlite3';before=path.read_bytes()
        with patch('execution.health_monitor.latest_refresh_snapshot',return_value=self.snapshot) as query:
            first=view.refresh();second=view.refresh()
        self.assertIs(first,second);query.assert_called_once();self.assertEqual(path.read_bytes(),before)

    def test_uncheckpointed_wal_change_invalidates_the_local_view(self):
        path=self.root/'data/execution/live_execution.sqlite3';view=FlowViewService(self.root,self.args)
        db=sqlite3.connect(path)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            with patch('execution.health_monitor.latest_refresh_snapshot',return_value=self.snapshot) as query:
                first=view.refresh()[0]['_flow_view_revision']
                db.execute('INSERT INTO fixture VALUES (1)');db.commit()
                second=view.refresh()[0]['_flow_view_revision']
            self.assertNotEqual(first,second);self.assertEqual(query.call_count,2)
        finally:db.close()

    def test_failed_broker_read_keeps_old_display_quantities_without_restoring_authority(self):
        view=FlowViewService(self.root,self.args)
        with patch('execution.health_monitor.latest_refresh_snapshot',return_value=self.snapshot):view.refresh()
        failed=dict(status='ERROR',errors=['offline'],broker_inventory_verified=False,inventory_comparison=[],
            checked_at=self.at.isoformat(),budget_status='BLOCKED',current_buy_budget_twd='0')
        save(self.state/'latest.json',dict(summary={},candidates=[]))
        with patch('execution.health_monitor.latest_refresh_snapshot',return_value=failed):health=view.refresh()[2]
        self.assertEqual(health['inventory_comparison'][0]['broker_cash_qty'],10)
        self.assertEqual(health['inventory_comparison'][0]['status'],'LAST_VERIFIED_NOT_CURRENT')
        self.assertFalse(health['broker_inventory_verified']);self.assertEqual(health['current_buy_budget_twd'],'0')
        self.assertEqual(health['inventory_display_checked_at'],self.snapshot['checked_at'])

    def test_observation_does_not_wait_for_the_controller_operation_lock(self):
        controller=FlowController(self.root,self.args);entered,release=Event(),Event()
        def hold():
            with controller.lock:entered.set();release.wait(2)
        worker=Thread(target=hold);worker.start();self.assertTrue(entered.wait(1))
        try:
            started=monotonic();self.assertEqual(controller.observation()['activity']['active'],[])
            self.assertLess(monotonic()-started,1)
        finally:release.set();worker.join(2)

    def test_manual_health_check_outside_market_does_not_request_intraday_quotes(self):
        controller=FlowController(self.root,self.args)
        runtime=SimpleNamespace(refresh=Mock(return_value=self.snapshot),can_trade_now=lambda:False,fetch_quotes=Mock())
        controller.runtime_action=lambda operation:operation(runtime,None)
        with patch('opportunity_flow.data.load_dataset',return_value=dict(grouped={},sessions=[])):
            report=controller.refresh_account()
        runtime.refresh.assert_called_once_with();runtime.fetch_quotes.assert_not_called()
        self.assertEqual(report['quote_check_status'],'OUTSIDE_TRADING_SESSION')


    def test_offhours_close_evidence_gets_provisional_pass_with_date(self):
        reference=dict(quote_status='DAILY_CLOSE_REFERENCE',display_only=True,execution_usable=False,last_price=100,price_as_of='2026-09-29')
        with patch('opportunity_flow.data.load_dataset',return_value=dict(grouped={'1111':[]},sessions=['2026-09-29'])):
            report=inspect_health(self.root,self.state,self.dataset,self.snapshot,controller_status='RUNNING',at=self.at,
                reference_quotes={'1111':reference})
            item=next(r for r in report['checks'] if r['key']=='quotes')
            self.assertEqual(item['status'],'PASS');self.assertTrue(item['asterisk']);self.assertIn('2026-09-29',item['detail'])
            report=inspect_health(self.root,self.state,self.dataset,self.snapshot,controller_status='RUNNING',at=self.at,
                reference_quotes={'1111':dict(reference,last_price=None)})
        self.assertEqual(next(r for r in report['checks'] if r['key']=='quotes')['status'],'WARN')


class ActivityTests(unittest.TestCase):
    def test_failed_background_job_clears_spinner_but_retains_error(self):
        log=ActivityLog()
        with self.assertRaises(TimeoutError):
            with log.track('BROKER_ACCOUNT','券商對帳'):
                self.assertEqual(log.snapshot()['active'][0]['label'],'券商對帳')
                raise TimeoutError('offline')
        self.assertEqual(log.snapshot()['active'],[])
        self.assertEqual(log.snapshot()['recent'][-1]['error'],'TimeoutError')

    def test_concurrent_work_can_finish_without_hiding_another_active_job(self):
        log=ActivityLog();entered,release=Event(),Event()
        def work():
            with log.track('RESEARCH','兩方研究'):entered.set();release.wait(2)
        worker=Thread(target=work);worker.start();self.assertTrue(entered.wait(1))
        try:
            with log.track('BROKER_ACCOUNT','券商對帳'):self.assertEqual(len(log.snapshot()['active']),2)
            self.assertEqual([r['kind'] for r in log.snapshot()['active']],['RESEARCH'])
        finally:release.set();worker.join(2)


if __name__=='__main__':unittest.main()
