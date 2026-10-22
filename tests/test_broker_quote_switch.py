"""Quote symbol changes must not churn the logged-in SKCOM event sink."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from execution.broker_runtime import BrokerRuntime


class BrokerQuoteSwitchTests(unittest.TestCase):
    def test_symbol_change_keeps_monitor_and_event_connection(self):
        calls = []
        markets = []

        class FakeMarket:
            def __init__(self, audit, stopped, login_session):
                self.audit = audit
                self.stopped = stopped
                self.entered = False
                self.subscribed = ()
                markets.append(self)
                calls.append('create')

            def event(self, kind, detail):
                calls.append(kind)

            def leave_monitor(self):
                calls.append('leave')
                self.entered = False
                self.subscribed = ()
                return 0

            def fetch_batch(self, symbols, timeout):
                calls.append(('fetch', symbols, self.subscribed, self.stopped()))
                self.subscribed = symbols
                self.entered = len(symbols) < 20
                return symbols

            def close(self):
                calls.append('close')

        with tempfile.TemporaryDirectory() as temporary:
            fake_com = SimpleNamespace(CoInitialize=Mock(), CoUninitialize=Mock())
            session = SimpleNamespace(
                connect=Mock(),
                ensure_ready=Mock(return_value=SimpleNamespace(
                    ready=True, action='REPLY_RECONNECTED', reply_state=1)),
            )
            with patch.dict(sys.modules, {'comtypes': fake_com}), patch(
                'execution.broker_runtime.build_live_readonly_session_from_project',
                return_value=session,
            ), patch('capital_multi_quote.MultiCapitalMarket', FakeMarket):
                runtime = BrokerRuntime(Path(temporary))
                try:
                    self.assertEqual(('A', 'B'), runtime.fetch_quotes(
                        ['A', 'B'], object(), lambda: False))
                    self.assertEqual(tuple(str(i) for i in range(21)), runtime.fetch_quotes(
                        [str(i) for i in range(21)], object(), lambda: True))
                    self.assertEqual(tuple(str(i) for i in range(21)), runtime.fetch_quotes(
                        [str(i) for i in range(21)], object(), lambda: False))
                finally:
                    runtime.shutdown()

        self.assertEqual(1, len(markets))
        self.assertEqual(1, calls.count('create'))
        self.assertEqual(1, calls.count('leave'))
        self.assertEqual(1, calls.count('close'))  # final shutdown only
        self.assertLess(calls.index('leave'), calls.index(('fetch', tuple(
            str(i) for i in range(21)), (), True)))
        self.assertEqual(2, session.ensure_ready.call_count)
        fake_com.CoUninitialize.assert_called_once()


if __name__ == '__main__':
    unittest.main()
