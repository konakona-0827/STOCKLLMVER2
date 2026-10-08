from __future__ import annotations

import unittest
from decimal import Decimal

from analysis_service import live_buy_budget
from dashboard import decision_text


class LiveBuyBudgetTests(unittest.TestCase):
    def test_budget_is_lowest_of_broker_strategy_and_daily_remaining(self):
        result = live_buy_budget(
            dict(status='OK', errors=[], broker_balance_twd='30541', minimum_balance_twd='10000'),
            Decimal('12000'), Decimal('10000'), Decimal('20000'))
        self.assertEqual(Decimal('8000'), result['available'])
        self.assertEqual('READY', result['status'])

    def test_unconfirmed_daily_reservations_fail_closed(self):
        result = live_buy_budget(
            dict(status='OK', errors=[], broker_balance_twd='30541', minimum_balance_twd='10000'),
            None, Decimal('10000'), Decimal('20000'))
        self.assertEqual(Decimal('0'), result['available'])
        self.assertEqual('BLOCKED', result['status'])

    def test_unhealthy_broker_fails_closed(self):
        result = live_buy_budget(
            dict(status='ERROR', errors=['session unavailable'], broker_balance_twd=None,
                 minimum_balance_twd='10000'),
            Decimal('0'), Decimal('10000'), Decimal('20000'))
        self.assertEqual(Decimal('0'), result['available'])
        self.assertEqual('BLOCKED', result['status'])

    def test_dashboard_displays_checked_budget_and_buy_estimate(self):
        rendered = decision_text(
            dict(market_view='測試', decisions=[dict(symbol='0050', decision='BUY',
                suggested_qty=2, confidence=0.8, action_ratio=0, warnings=[])]),
            [dict(symbol='0050', ask=100, last_price=99, asset_type='ETF')],
            '2026-10-08T12:00:00+08:00',
            dict(available_cash_twd=500, broker_buying_power_twd=20541,
                 minimum_balance_reserve_twd=10000, daily_buy_remaining_twd=20000,
                 min_buy_fee_reserve_twd=20, buy_cash_buffer_rate=0.01,
                 budget_status='READY'))
        self.assertTrue(rendered.startswith('AI 分析時間：2026-10-08T12:00:00+08:00'))
        self.assertIn('本輪可用買進預算：NT$ 500', rendered)
        self.assertIn('AI BUY 估算總額（含預留）：NT$ 220', rendered)
        self.assertIn('本筆預估委託（含費用預留）：NT$ 220', rendered)


if __name__ == '__main__':
    unittest.main()
