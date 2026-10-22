from datetime import date
from unittest.mock import patch
import unittest

import daily_history


class DailyHistoryTests(unittest.TestCase):
    def test_official_rows_require_valid_complete_ohlcv(self):
        payload = dict(stat='OK', title='115年10月 2330 台積電 各日成交資訊',
                       fields=['日期', '成交股數', '開盤價', '最高價', '最低價', '收盤價'],
                       data=[['115/10/14', '1,000', '100', '105', '99', '104'],
                             ['115/10/15', '0', '--', '--', '--', '--'],
                             ['115/09/30', '1,000', '100', '105', '99', '104']])
        bars = daily_history._parse_month(payload, '2330', date(2026, 10, 1),
                                          '2026-10-15T10:00:00+08:00', 'https://twse.example')
        self.assertEqual([bar['date'] for bar in bars], ['2026-10-14'])
        self.assertEqual(bars[0]['source'], 'TWSE_DAILY')

    def test_as_of_filter_prevents_today_and_future_leakage(self):
        days = [date(2026, 10, d) for d in (7, 8, 12, 13, 14, 15, 16)]
        bars = [dict(date=day.isoformat(), open=100, high=105, low=95,
                     close=100, volume=1000, complete=True,
                     source='TWSE_DAILY') for day in days]
        with patch.object(daily_history, '_fetch_month', return_value=(bars, None)):
            result = daily_history.load_many(['2330'], date(2026, 10, 15))['2330']
        self.assertEqual(result['status']['latest_date'], '2026-10-14')
        self.assertEqual(len(result['bars']), 5)
        candidate = dict(symbol='2330', market_history=[])
        quote = dict(symbol='2330', quote_status='LIVE', is_trial=False,
                     quote_timestamp='2026-10-15T10:00:00+08:00',
                     last_price=100, open=99, high=102, low=95,
                     bid=99, ask=100, volume=1000)
        with patch.object(daily_history, 'load_many', return_value={'2330': result}):
            daily_history.enrich_candidates([candidate], {'2330': quote},
                                            date(2026, 10, 15))
        self.assertIn('multi_day_trend', candidate['eligible_strategies'])
        self.assertEqual(candidate['daily_history_status']['prior_complete_sessions'], 5)


if __name__ == '__main__':
    unittest.main()
