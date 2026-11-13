from datetime import datetime,timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from execution.timing import ExecutionTrace,trace_scope,span,mark,observe_fill,instrument_execution,current_trace
from execution.broker_runtime import BrokerRuntime
from opportunity_flow.common import TAIPEI
from opportunity_flow.health import inspect_health


class ExecutionTimingTests(unittest.TestCase):
    def test_monotonic_duration_survives_backwards_wall_clock(self):
        wall=datetime(2026,11,13,12,tzinfo=TAIPEI);ticks=count()
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return wall-timedelta(seconds=next(ticks))
        ns=count(0,1_000_000)
        with patch('execution.timing.datetime',Clock),patch('execution.timing.perf_counter_ns',side_effect=lambda:next(ns)):
            with trace_scope() as trace:
                with span('query'):pass
                result=trace.export()
        events=result['events'];self.assertLess(events[1]['at'],events[0]['at'])
        self.assertGreaterEqual(events[1]['duration_ms'],0)
        self.assertGreaterEqual(events[1]['offset_ms'],events[0]['offset_ms'])

    def test_file_logging_failure_preserves_the_original_outcome_and_exception(self):
        @instrument_execution
        def work(*,project_root):return {'results':[],'status':'UNCHANGED'}
        with tempfile.TemporaryDirectory() as root,patch('pathlib.Path.open',side_effect=PermissionError('locked')):
            result=work(project_root=root)
            self.assertEqual(result['status'],'UNCHANGED');self.assertIn('PermissionError',result['execution_timing']['write_errors'])
            with trace_scope(),self.assertRaisesRegex(ValueError,'original failure'):
                with span('failing'):raise ValueError('original failure')

    def test_trace_crosses_the_original_broker_thread_without_leaking_to_next_jobs(self):
        found=[]
        def execute(*args,**kwargs):
            found.append(current_trace().id);mark('isolated_pipeline');return {'status':'TEST_ONLY'}
        with tempfile.TemporaryDirectory() as root,patch.dict(sys.modules,{'comtypes':SimpleNamespace(CoInitialize=Mock(),CoUninitialize=Mock())}),patch(
            'execution.broker_runtime.build_live_readonly_session_from_project',return_value=SimpleNamespace(connect=Mock()),
        ),patch('execution.pipeline_hook.execute_after_advice_written',side_effect=execute),patch(
            'execution.health_monitor.run_health_check',side_effect=lambda *a,**k:{'trace_is_none':current_trace() is None},
        ):
            runtime=BrokerRuntime(root)
            try:
                with trace_scope() as trace:
                    trace.enable(root);runtime.execute(Path(root)/'never-real.json');events=trace.export()['events']
                self.assertEqual(found,[trace.id]);self.assertTrue(runtime.health()['trace_is_none'])
                self.assertTrue(any(e['stage']=='broker_worker_started' and e['queue_wait_ms']>=0 for e in events))
            finally:runtime.shutdown()

    def test_slippage_uses_broker_priced_fills_instead_of_order_limit(self):
        fields=['']*21;fields[0],fields[1],fields[2],fields[3]='1234567890123','TC','D','N'
        fields[6],fields[8],fields[11],fields[20]='B','0050','100','2'
        result=SimpleNamespace(symbol='0050',seq13='1234567890123',price=Decimal('999'),filled_quantity=2,crosscheck=SimpleNamespace(fill_rows=[','.join(fields)]))
        with trace_scope() as trace:
            observe_fill(result,SimpleNamespace(ask=Decimal('99.9'),bid=Decimal('99.8')),'BUY')
            event=trace.export()['events'][-1]
        self.assertTrue(event['verified']);self.assertEqual(Decimal(event['fill_vwap']),Decimal(100))
        self.assertGreater(Decimal(event['realized_slippage_bps']),0)
        with trace_scope() as trace:
            result.crosscheck.fill_rows=['no priced trade evidence'];observe_fill(result,SimpleNamespace(ask=100,bid=99),'SELL')
            event=trace.export()['events'][-1]
        self.assertFalse(event['verified']);self.assertIsNone(event['realized_slippage_bps'])

    def test_repaired_broker_mismatch_keeps_before_and_after_evidence(self):
        from execution.health_monitor import sync_broker_state
        at=datetime.now(TAIPEI).isoformat()
        initial=dict(checked_at=at,inventory_checked=True,status='WARN',errors=[],inventory_comparison=[dict(symbol='1111',status='UNATTRIBUTED_BROKER')])
        final=dict(checked_at=at,inventory_checked=True,status='OK',errors=[],inventory_comparison=[dict(symbol='1111',status='MATCH')])
        repair=dict(symbol='1111',previous_user_qty=0,previous_ai_qty=0,user_qty=10,ai_qty=0,broker_cash_qty=10,broker_checked_at=at)
        broker=SimpleNamespace(health=Mock(side_effect=[initial,final]))
        with tempfile.TemporaryDirectory() as root,patch('execution.health_monitor.reconcile_confirmed_broker_events',return_value=dict(updated=[],errors=[])),patch(
            'execution.position_configuration.reconcile_broker_inventory',return_value=[repair],
        ),patch('execution.health_monitor.persist_refresh_snapshot',side_effect=lambda root,health:health),patch('execution.health_monitor.time.sleep'):
            _,snapshot=sync_broker_state(root,runtime=broker,return_report=True)
        receipt=snapshot['reconciliation_receipt']
        self.assertEqual(receipt['state'],'REPAIRED');self.assertEqual(len(receipt['initial_differences']),1)
        self.assertEqual(receipt['position_repairs'],[repair]);self.assertEqual(receipt['remaining_differences'],[])

    def test_unverified_broker_never_overwrites_local_holdings(self):
        from execution.health_monitor import sync_broker_state
        report=dict(checked_at=datetime.now(TAIPEI).isoformat(),inventory_checked=False,status='ERROR',errors=['broker unavailable'],inventory_comparison=[])
        with tempfile.TemporaryDirectory() as root,patch('execution.health_monitor.reconcile_confirmed_broker_events',return_value=dict(updated=[],errors=[])),patch(
            'execution.health_monitor.persist_refresh_snapshot',side_effect=lambda root,health:health,
        ):
            _,snapshot=sync_broker_state(root,runtime=SimpleNamespace(health=lambda:report),return_report=True)
            self.assertFalse((Path(root)/'data/execution/live_execution.sqlite3').exists())
        self.assertEqual(snapshot['reconciliation_receipt']['state'],'BROKER_UNVERIFIED')
        self.assertEqual(snapshot['reconciliation_receipt']['fallback'],'KEEP_CONFIRMED_LOCAL_RECORDS_AND_BLOCK_UNVERIFIED_TRADES')

    def test_offline_summary_keeps_an_uncertain_send_and_partial_log_explicit(self):
        from operations.review_execution_timing import summarize
        import json
        event=dict(trace_id='fixture',sequence=1,stage='sdk_send',phase='start',at=datetime.now(TAIPEI).isoformat(),
            offset_ms=1,decision_id='fixture-order',symbol='0050',side='BUY')
        with tempfile.TemporaryDirectory() as root:
            broken=[dict(event,sequence=[],duration_ms='bad'),dict(event,sequence=2,phase='end',duration_ms='bad'),
                    dict(event,sequence=3,queue_wait_ms=float('nan')),dict(event,sequence=4,decision_id=[])]
            (Path(root)/'fixture.jsonl').write_text('\n'.join(json.dumps(e) for e in [event,*broken])+'\n{unfinished',encoding='utf-8')
            report=summarize(root)
        trace=report['traces'][0];row=trace['decisions'][0]
        self.assertTrue(trace['partial_record']);self.assertTrue(row['sdk_call_started'])
        self.assertFalse(row['sdk_return_observed']);self.assertIsNone(row['fill_observation'])


if __name__=='__main__':unittest.main()
