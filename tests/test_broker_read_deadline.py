"""Read deadlines quarantine the COM owner without orders or a second login."""
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from execution.broker_runtime import BrokerRuntime
from state_lock import StateLease


class BrokerReadDeadlineTests(unittest.TestCase):
    @contextmanager
    def runtime(self, root, health, release):
        session = SimpleNamespace(connect=Mock())
        with patch.dict(sys.modules, {'comtypes': SimpleNamespace(CoInitialize=Mock(), CoUninitialize=Mock())}), patch(
            'execution.broker_runtime.build_live_readonly_session_from_project', return_value=session,
        ) as build, patch('execution.health_monitor.run_health_check', side_effect=health), patch(
            'execution.pipeline_hook.execute_after_advice_written', return_value={'status':'COMPLETE'},
        ) as execute:
            runtime = BrokerRuntime(root)
            try:
                yield runtime, build, execute
            finally:
                release.set()
                runtime.shutdown()
                runtime._thread.join(timeout=2)
                self.assertFalse(runtime._thread.is_alive())

    def test_timeout_blocks_new_and_already_queued_execution_without_relogin(self):
        release = Event()
        queued = Future()
        holder = {}
        def health(*args, **kwargs):
            holder['runtime']._queue.put(('execute', (Path('never-send.json'),), queued))
            release.wait(2)
            return {'status':'OK'}
        with tempfile.TemporaryDirectory() as root, self.runtime(root, health, release) as (runtime, build, execute):
            holder['runtime'] = runtime
            with patch.object(runtime, '_read_wait_seconds', return_value=.02):
                with self.assertRaisesRegex(TimeoutError, 'DEADLINE_EXCEEDED'):
                    runtime.health()
            with self.assertRaisesRegex(RuntimeError, 'RESTART_REQUIRED'):
                runtime.execute(Path('also-never-send.json'))
            release.set()
            with self.assertRaisesRegex(RuntimeError, 'RESTART_REQUIRED'):
                queued.result(timeout=2)
            execute.assert_not_called()
            build.assert_called_once()

    def test_late_success_is_rejected_after_a_clock_gap(self):
        release = Event()
        clock = [0]
        def health(*args, **kwargs):
            clock[0] = 600
            return {'status':'OK'}
        with tempfile.TemporaryDirectory() as root, self.runtime(root, health, release) as (runtime, _, execute):
            with patch('execution.broker_runtime.monotonic', side_effect=lambda:clock[0]):
                with self.assertRaisesRegex(TimeoutError, 'DEADLINE_EXCEEDED'):
                    runtime.health()
            with self.assertRaisesRegex(RuntimeError, 'RESTART_REQUIRED'):
                runtime.health()
            execute.assert_not_called()

    def test_cancelled_queued_quote_is_skipped_without_killing_the_owner(self):
        release, began = Event(), Event()
        def health(*args, **kwargs):
            began.set()
            release.wait(2)
            return {'status':'OK'}
        with tempfile.TemporaryDirectory() as root, self.runtime(root, health, release) as (runtime, _, execute), patch(
            'capital_multi_quote.MultiCapitalMarket',
        ) as market:
            busy = Future()
            runtime._queue.put(('health', (), busy))
            self.assertTrue(began.wait(1))
            with patch.object(runtime, '_read_wait_seconds', return_value=.02):
                quotes, errors = runtime.fetch_quotes_with_markets(['0050'], {'0050':5}, None, lambda:False)
            self.assertEqual({}, quotes)
            self.assertIn('DEADLINE_EXCEEDED', errors['0050'])
            release.set()
            busy.result(timeout=2)
            self.assertEqual('STOPPED', runtime.shutdown()['status'])
            market.assert_not_called()
            execute.assert_not_called()

    def test_shutdown_returns_without_releasing_a_busy_com_owner_lease(self):
        release = Event()
        def health(*args, **kwargs):
            release.wait(2)
            return {'status':'OK'}
        with tempfile.TemporaryDirectory() as root, self.runtime(root, health, release) as (runtime, _, execute):
            with patch.object(runtime, '_read_wait_seconds', return_value=.02):
                with self.assertRaises(TimeoutError): runtime.health()
                report = runtime.shutdown()
            self.assertEqual('STOPPING_BROKER_UNRESPONSIVE', report['status'])
            self.assertTrue(report['lease_held'])
            with self.assertRaises(RuntimeError): StateLease(root, 'broker-runtime').acquire()
            execute.assert_not_called()
            release.set()
            runtime._thread.join(timeout=2)
            with StateLease(root, 'broker-runtime'): pass


if __name__ == '__main__':
    unittest.main()
