"""Reading position and exchange-calendar context for analysis."""
from datetime import date, datetime
import json
import unittest
from unittest.mock import patch

from config import TAIPEI
from dashboard import update_detail_text
from market_calendar import previous_trading_day, trading_day_context
from position_analyzer import build_request, validate


class DetailWidget:
    def __init__(self, content, position):
        self.content = content
        self.position = position
        self.replacements = 0

    def get(self, *_):
        return self.content

    def yview(self):
        return (self.position, min(self.position + .2, 1))

    def configure(self, **_):
        pass

    def delete(self, *_):
        self.replacements += 1
        self.content = ''

    def insert(self, _index, content):
        self.content = content

    def yview_moveto(self, position):
        self.position = position


class ReadingAndCalendarTests(unittest.TestCase):
    def test_periodic_repaint_keeps_position_and_does_not_rewrite_same_text(self):
        widget = DetailWidget('舊分析' * 100, .62)
        update_detail_text(widget, widget.content)
        self.assertEqual(widget.replacements, 0)
        self.assertEqual(widget.position, .62)
        update_detail_text(widget, '新分析' * 100)
        self.assertEqual(widget.replacements, 1)
        self.assertEqual(widget.position, .62)

    def test_previous_day_skips_weekend_and_exchange_holiday(self):
        self.assertEqual(previous_trading_day(date(2026, 10, 12)), date(2026, 10, 8))
        self.assertEqual(previous_trading_day(date(2026, 10, 13)), date(2026, 10, 12))
        context = trading_day_context(date(2026, 10, 12))
        self.assertEqual(context['previous_trading_date'], '2026-10-08')
        self.assertEqual([item['date'] for item in context['skipped_closed_dates']],
                         ['2026-10-09', '2026-10-10', '2026-10-11'])

    def test_ai_request_explains_holiday_without_inventing_daily_bars(self):
        clock = datetime(2026, 10, 12, 11, 45, tzinfo=TAIPEI)
        candidate = {'symbol': '0050', 'market_history': [
            {'exchange_time': '2026-10-08T11:20:00+08:00', 'price': 100}]}
        with patch('position_analyzer.now', return_value=clock):
            request = build_request([candidate], strategy_prompt='測試策略')
        payload = json.loads(request['input'])
        self.assertEqual(payload['trading_calendar']['previous_trading_date'], '2026-10-08')
        self.assertEqual(payload['analysis_set'][0]['market_history'], candidate['market_history'])
        self.assertIn('休市日不構成缺口', request['instructions'])
        self.assertIn('不是完整日 K', request['instructions'])
        self.assertIn('不是禁止 BUY 的條件', request['instructions'])

    def test_missing_daily_bars_is_not_a_local_buy_rejection(self):
        candidate = {'symbol': '0050', 'last_price': 100, 'quote_status': 'LIVE',
                     'quote_timestamp': '2026-10-12T11:00:00+08:00',
                     'ask': 100, 'bid': 99.9, 'max_buy_qty': 2,
                     'asset_type': 'ETF', 'eligible_strategies': ['intraday_recovery'],
                     'paper_position': {'qty': 0},
                     'metrics': {'history_status': 'INTRADAY_SNAPSHOTS_ONLY'}}
        decision = {'market_view': '僅有盤中資料', 'decisions': [
            {'symbol': '0050', 'decision': 'BUY', 'confidence': .4,
             'action_ratio': 0, 'suggested_qty': 1,
             'reference_price': 100, 'suggested_price': 100,
             'strategy_id': 'intraday_recovery', 'target_price': 120,
             'invalidation_price': 95, 'holding_days': 10,
             'reason': '盤中即時行情支持小部位，成本與失效價位已評估',
             'data_quality': 'LIVE', 'warnings': ['預算限制']} ]}
        self.assertEqual(validate(decision, [candidate])['decisions'][0]['decision'], 'BUY')


if __name__ == '__main__':
    unittest.main()
