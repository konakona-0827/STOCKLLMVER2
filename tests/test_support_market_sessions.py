"""Actual market closures must not become missing stock sessions; real gaps must fail."""
from datetime import date, timedelta
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import daily_history
from support_scout.market_sessions import load_sessions, parse_sessions
from support_scout.trading import load_observation


class SupportMarketSessionsTests(unittest.TestCase):
    def test_market_parser_rejects_other_month_and_duplicate_days(self):
        payload = dict(stat='OK', fields=['日期', '成交金額', '發行量加權股價指數'],
                       data=[['115/07/09', '123,000', '22,000']])
        self.assertEqual(parse_sessions(payload, date(2026, 7, 1)), ['2026-07-09'])
        with self.assertRaisesRegex(ValueError, 'MONTH_MISMATCH'):
            parse_sessions(payload, date(2026, 6, 1))
        payload['data'] *= 2
        with self.assertRaisesRegex(ValueError, 'DUPLICATE'):
            parse_sessions(payload, date(2026, 7, 1))

    def test_market_tail_must_match_independent_latest_session(self):
        with patch('support_scout.market_sessions.fetch_month',
                   return_value=(['2026-10-21', '2026-10-22'], {})):
            sessions, status = load_sessions('2026-10-27', 2, Path('unused'))
        self.assertFalse(status['verified'])
        self.assertEqual(sessions, ['2026-10-21', '2026-10-22'])

    def fixture(self):
        cursor = date(2026, 6, 1)
        days = []
        while len(days) < 60:
            if cursor.weekday() < 5 and cursor != date(2026, 7, 10):
                days.append(cursor.isoformat())
            cursor += timedelta(days=1)
        bars = [dict(date=d, open=100, high=105, low=95, close=100, volume=1000000,
                     turnover_twd=100000000) for d in days]
        return days, dict(bars=bars, status=dict(expected_latest_date=days[-1],
            latest_date=days[-1], failed_months=[], latest_retrieved_at='2026-10-27T16:00:00+08:00'))

    def test_actual_market_closure_is_accepted_but_stock_missing_session_is_rejected(self):
        days, item = self.fixture()
        for missing in (False, True):
            actual = dict(item, bars=item['bars'][1:] if missing else item['bars'])
            with patch('daily_history.load_many', return_value={'2330': actual}), \
                    patch('support_scout.market_sessions.load_sessions',
                          return_value=(days, dict(verified=True))):
                result = load_observation('2330', dict(asset_type='STOCK'), 60, Path('unused'))
            self.assertEqual(result['status'], 'UNAVAILABLE' if missing else 'RAW_PRICE_OBSERVATION')
        self.assertNotIn('2026-07-10', days)

    def test_close_history_refreshes_same_day_morning_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / '2330_202610.json'
            path.write_text(json.dumps(dict(symbol='2330', month='202610',
                fetched_at='2026-10-27T09:00:00+08:00', bars=[dict(date='2026-10-22')])), encoding='utf-8')
            payload = dict(stat='OK', title='2330', fields=['日期','成交股數','成交金額',
                '開盤價','最高價','最低價','收盤價'],
                data=[['115/10/27','1000','100000','100','105','95','100']])
            with patch('daily_history.urlopen', return_value=BytesIO(json.dumps(payload).encode())) as fetch:
                bars, error = daily_history._fetch_month('2330', date(2026,10,1), directory,
                    date(2026,10,27), minimum_latest='2026-10-27')
            self.assertIsNone(error)
            self.assertEqual(bars[-1]['date'], '2026-10-27')
            self.assertEqual(fetch.call_count, 1)


if __name__ == '__main__':
    unittest.main()
