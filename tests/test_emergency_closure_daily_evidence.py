"""A verified exchange closure is not a missing daily bar or a trading day."""
from datetime import date
import unittest
from unittest.mock import patch

import daily_history
from market_calendar import previous_trading_day, trading_day_context, twse_closed_reason
from pretrade_policy import price_levels, cooldown_active
from strategy_evidence import assess


class EmergencyClosureEvidenceTests(unittest.TestCase):
    def bars(self):
        cursor = date(2026, 10, 23)
        rows = []
        for index in range(22):
            cursor = previous_trading_day(cursor)
            rows.append(dict(date=cursor.isoformat(), open=100, high=110+index,
                low=90-index, close=100, volume=10000, complete=True, source='TWSE_DAILY'))
        return sorted(rows, key=lambda row: row['date'])

    def candidate(self, bars):
        return dict(symbol='2330', last_price=100, bid=99.9, ask=100,
            open=99, high=101, low=98, volume=10000, quote_status='LIVE', is_trial=False,
            quote_timestamp='2026-10-27T11:00:00+08:00', daily_bars=bars)

    def test_previous_session_and_skipped_dates_include_typhoon_closure(self):
        self.assertIsNotNone(twse_closed_reason(date(2026, 10, 23)))
        self.assertEqual(previous_trading_day(date(2026, 10, 27)), date(2026, 10, 22))
        context = trading_day_context(date(2026, 10, 27))
        self.assertEqual([row['date'] for row in context['skipped_closed_dates']],
                         ['2026-10-23', '2026-10-24', '2026-10-25', '2026-10-26'])

    def test_complete_history_restores_daily_strategy_and_verified_price_levels(self):
        with patch.object(daily_history, '_fetch_month', return_value=(self.bars(), None)):
            item = daily_history.load_many(['2330'], date(2026, 10, 27))['2330']
        self.assertEqual(item['status']['expected_latest_date'], '2026-10-22')
        self.assertEqual(item['status']['coverage'], 'SUFFICIENT_FOR_20D_COMPARISON')
        candidate = self.candidate(item['bars'])
        self.assertIn('multi_day_trend', assess(candidate)['eligible_strategies'])
        levels = {row['id'] for row in price_levels(candidate)}
        self.assertTrue({'previous_close', 'high_5d', 'low_5d', 'high_20d', 'low_20d'} <= levels)

    def test_genuinely_missing_previous_session_is_still_stale(self):
        bars = [row for row in self.bars() if row['date'] != '2026-10-22']
        with patch.object(daily_history, '_fetch_month', return_value=(bars, None)):
            item = daily_history.load_many(['2330'], date(2026, 10, 27))['2330']
        self.assertEqual(item['status']['coverage'], 'STALE')
        candidate = self.candidate(item['bars'])
        self.assertNotIn('multi_day_trend', assess(candidate)['eligible_strategies'])
        self.assertNotIn('previous_close', {row['id'] for row in price_levels(candidate)})

    def test_reentry_cooldown_counts_real_sessions_not_the_closed_friday(self):
        self.assertTrue(cooldown_active([dict(side='SELL', date='2026-10-20', quantity=1)],
                                       date(2026, 10, 27)))
        self.assertFalse(cooldown_active([dict(side='SELL', date='2026-10-19', quantity=1)],
                                        date(2026, 10, 27)))


if __name__ == '__main__':
    unittest.main()
