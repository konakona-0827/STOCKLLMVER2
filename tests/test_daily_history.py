from datetime import date
from pathlib import Path
import json
import tempfile
from unittest.mock import patch
import unittest

import daily_history


class DailyHistoryTests(unittest.TestCase):
    def test_previous_month_morning_cache_cannot_hide_a_missing_final_session(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=Path(directory)
            record=dict(symbol='0050',month='202610',fetched_at='2026-10-30T10:35:00+08:00',
                        bars=[dict(date='2026-10-29')])
            (cache/'0050_202610.json').write_text(json.dumps(record),encoding='utf-8')
            with patch.object(daily_history,'urlopen',side_effect=OSError('offline')) as fetch:
                rows,error=daily_history._fetch_month('0050',date(2026,10,1),cache,date(2026,11,3),
                                                    minimum_latest='2026-11-02')
            fetch.assert_called_once()
            self.assertEqual(error,'OSError')
            self.assertEqual(rows,record['bars'])

    def test_finalized_past_month_can_be_reused_without_repeated_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=Path(directory)
            record=dict(symbol='0050',month='202610',fetched_at='2026-11-01T10:35:00+08:00',
                        bars=[dict(date='2026-10-30')])
            (cache/'0050_202610.json').write_text(json.dumps(record),encoding='utf-8')
            with patch.object(daily_history,'urlopen',side_effect=AssertionError('no network')) as fetch:
                rows,error=daily_history._fetch_month('0050',date(2026,10,1),cache,date(2026,11,3),
                                                    minimum_latest='2026-11-02')
            fetch.assert_not_called()
            self.assertIsNone(error)
            self.assertEqual(rows,record['bars'])

    def test_latest_date_and_twenty_two_rows_do_not_prove_contiguous_daily_history(self):
        cursor=date(2026,11,2); days=[]
        for _ in range(23):
            days.append(cursor)
            cursor=daily_history.previous_trading_day(cursor)
        bars=[dict(date=d.isoformat(),open=100,high=105,low=95,close=100,volume=1000,
                   complete=True,source='TWSE_DAILY') for d in days if d!=date(2026,10,30)]
        with patch.object(daily_history,'_fetch_month',return_value=(bars,None)):
            item=daily_history.load_many(['0050'],date(2026,11,3))['0050']
        self.assertEqual(len(item['bars']),22)
        self.assertEqual(item['status']['latest_date'],'2026-11-02')
        self.assertEqual(item['status']['missing_recent_trading_dates'],['2026-10-30'])
        self.assertEqual(item['status']['coverage'],'PARTIAL')

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
