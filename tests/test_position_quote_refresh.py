"""Automatic holdings quotes use the same broker worker and never call an LLM."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from analysis_service import AnalysisService
from config import TAIPEI
from dashboard import Dashboard, current_position_quotes


class PositionQuoteRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = AnalysisService(Path(self.tmp.name))
        self.at = datetime(2026, 11, 2, 10, 0, tzinfo=TAIPEI)
        self.runtime = Mock()
        self.health = dict(inventory_checked=True, inventory_comparison=[
            dict(symbol='1101', broker_cash_qty=99),
            dict(symbol='2449', broker_cash_qty=3),
            dict(symbol='0000', broker_cash_qty=0)])
        self.runtime.fetch_quotes.return_value = ({s: dict(
            symbol=s, source='CAPITAL_SKCOM', exchange_time=self.at.isoformat(),
            price=100, bid=99.9, ask=100, is_trial=False)
            for s in ('1101', '2449')}, {})

    def sync(self, root, *, enrich):
        enrich(self.health, self.runtime)
        return self.health

    def refresh(self, **kwargs):
        with (patch('execution.health_monitor.sync_broker_state', side_effect=self.sync),
              patch('analysis_service.now', return_value=self.at),
              patch('capital_multi_quote.now', return_value=self.at)):
            return self.service.refresh_broker_state(**kwargs)

    def test_automatic_health_worker_fetches_held_quotes_without_analysis(self):
        app = Mock(health_running=False, service=self.service)
        with (patch('dashboard.threading.Thread') as thread,
              patch('dashboard.time.time', return_value=self.at.timestamp()),
              patch('execution.health_monitor.sync_broker_state', side_effect=self.sync),
              patch('analysis_service.now', return_value=self.at),
              patch('capital_multi_quote.now', return_value=self.at)):
            Dashboard.launch_health(app)
            thread.call_args.kwargs['target']()
        self.runtime.fetch_quotes.assert_called_once()
        self.assertEqual(['1101', '2449'], self.runtime.fetch_quotes.call_args.args[0])
        event, snapshot = app.events.put.call_args.args[0]
        self.assertEqual('health', event)
        self.assertEqual(2, len(snapshot['position_quotes']))
        self.assertEqual({}, snapshot['position_quote_errors'])
        with patch('dashboard.now', return_value=self.at):
            self.assertEqual(2, len(current_position_quotes(snapshot, None)))

    def test_failure_clears_old_quotes_and_preserves_error(self):
        self.health['position_quotes'] = [dict(symbol='OLD')]
        self.runtime.fetch_quotes.side_effect = RuntimeError('quote disconnected')
        result = self.refresh()
        self.assertEqual([], result['position_quotes'])
        self.assertIn('quote disconnected', result['position_quote_errors']['batch'])

    def test_partial_missing_quote_and_trial_are_explicit(self):
        raw, _ = self.runtime.fetch_quotes.return_value
        raw.pop('2449')
        raw['1101']['is_trial'] = True
        result = self.refresh()
        self.assertEqual({'1101', '2449'}, set(result['position_quote_errors']))
        with patch('dashboard.now', return_value=self.at):
            self.assertEqual({}, current_position_quotes(result, None))

    def test_empty_inventory_clears_prior_quotes_without_subscription(self):
        self.health.update(inventory_comparison=[], position_quotes=[dict(symbol='OLD')])
        self.assertEqual([], self.refresh()['position_quotes'])
        self.runtime.fetch_quotes.assert_not_called()

    def test_closed_market_refresh_skips_quote_subscription(self):
        self.refresh(refresh_quotes=False)
        self.runtime.fetch_quotes.assert_not_called()

    def test_market_refresh_interval_is_one_minute_and_stale_quotes_stay_rejected(self):
        app = Mock(last_health=self.health)
        with patch('dashboard.time.time', return_value=self.at.timestamp()):
            self.assertEqual(60, Dashboard.health_delay(app))
        with patch('dashboard.time.time', return_value=self.at.replace(hour=15).timestamp()):
            # Idle off-hours checks resume at the next weekday 08:00; current
            # quote freshness is still enforced independently below.
            from scheduler import health_check_due_or_next
            wall=self.at.replace(hour=15).timestamp()
            self.assertEqual(health_check_due_or_next(wall)-wall,Dashboard.health_delay(app))
        snapshot = self.refresh()
        with patch('dashboard.now', return_value=self.at + timedelta(seconds=121)):
            self.assertEqual({}, current_position_quotes(snapshot, None))


if __name__ == '__main__':
    unittest.main()
