from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest

from dashboard import (confirmed_trade_gross, settlement_snapshot_current,
                       settlement_windows)


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

    def test_due_date_moves_out_of_future_without_being_marked_paid(self):
        grouped = {
            date(2026, 10, 13): [(date(2026, 10, 8), Decimal('-6021'))],
            date(2026, 10, 14): [(date(2026, 10, 12), Decimal('-224'))],
        }
        before = settlement_windows(grouped, date(2026, 10, 12))
        self.assertEqual(before['future']['payable'], Decimal('6245'))
        on_due_day = settlement_windows(grouped, date(2026, 10, 13))
        self.assertEqual(on_due_day['future']['payable'], Decimal('224'))
        self.assertEqual(on_due_day['today']['payable'], Decimal('6021'))
        after = settlement_windows(grouped, date(2026, 10, 14))
        self.assertEqual(after['past']['payable'], Decimal('6021'))
        self.assertEqual(after['today']['payable'], Decimal('224'))

    def test_opposite_cash_flows_net_per_settlement_day(self):
        grouped = {date(2026, 10, 14): [
            (date(2026, 10, 12), Decimal('-2100')),
            (date(2026, 10, 12), Decimal('1885'))]}
        windows = settlement_windows(grouped, date(2026, 10, 12))
        self.assertEqual(windows['future']['payable'], Decimal('215'))
        self.assertEqual(windows['future']['receivable'], Decimal('0'))

    def test_stale_snapshot_cannot_be_labeled_current(self):
        taipei = timezone(timedelta(hours=8))
        at = datetime(2026, 10, 13, 9, 30, tzinfo=taipei)
        self.assertTrue(settlement_snapshot_current('2026-10-13T09:20:00+08:00', at))
        self.assertFalse(settlement_snapshot_current('2026-10-12T17:00:00+08:00', at))
        self.assertFalse(settlement_snapshot_current('2026-10-13T09:00:00+08:00', at))


if __name__ == '__main__':
    unittest.main()
