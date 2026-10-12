from datetime import date
from decimal import Decimal
import unittest

from dashboard import confirmed_trade_gross


class DashboardSettlementSummaryTests(unittest.TestCase):
    def test_unknown_fill_date_is_not_assigned_from_order_date(self):
        checks = [dict(
            side='BUY', broker_verified_filled_qty=10,
            started_at='2026-10-08T09:30:00+08:00',
            broker_replay_fill_details=[dict(quantity=10, price_twd='188.5')],
        )]
        totals, by_date, undated, incomplete = confirmed_trade_gross(checks)
        self.assertEqual(totals, {'BUY': Decimal('1885'), 'SELL': Decimal('0')})
        self.assertEqual(by_date, {})
        self.assertTrue(undated)
        self.assertFalse(incomplete)

    def test_only_explicit_fill_dates_enter_daily_breakdown(self):
        checks = [dict(
            side='SELL', broker_verified_filled_qty=2,
            broker_replay_fill_details=[
                dict(quantity=1, price_twd='188.5', trade_date='2026-10-12'),
                dict(quantity=1, price_twd='189', trade_date='2026-10-13'),
            ],
        )]
        totals, by_date, undated, incomplete = confirmed_trade_gross(checks)
        self.assertEqual(totals['SELL'], Decimal('377.5'))
        self.assertEqual(by_date[date(2026, 10, 12)]['SELL'], Decimal('188.5'))
        self.assertEqual(by_date[date(2026, 10, 13)]['SELL'], Decimal('189'))
        self.assertFalse(undated)
        self.assertFalse(incomplete)

    def test_bad_fill_does_not_create_partial_total(self):
        checks = [dict(
            side='BUY', broker_verified_filled_qty=2,
            broker_replay_fill_details=[
                dict(quantity=1, price_twd='100', trade_date='2026-10-12'),
                dict(quantity=1, price_twd=None, trade_date='2026-10-12'),
            ],
        )]
        totals, by_date, _, incomplete = confirmed_trade_gross(checks)
        self.assertEqual(totals['BUY'], Decimal('0'))
        self.assertEqual(by_date, {})
        self.assertTrue(incomplete)


if __name__ == '__main__':
    unittest.main()
