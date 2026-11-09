import unittest

from operations.replay_archived_plans import daily_plan_outcome, find_quote_entry, sessions_after


class ArchivedPlanReplayTests(unittest.TestCase):
    def bar(self, day, op=100, high=104, low=96, close=100):
        return dict(date=day,open=op,high=high,low=low,close=close)

    def test_horizon_uses_later_trading_sessions_including_emergency_closure(self):
        self.assertEqual(sessions_after('2026-10-22',3),['2026-10-27','2026-10-28','2026-10-29'])

    def test_entry_day_extrema_never_create_a_same_day_oddlot_win(self):
        bars=[self.bar('2026-10-13',high=120,low=80),self.bar('2026-10-14')]
        result=daily_plan_outcome('2026-10-13',110,90,1,bars)
        self.assertEqual(result['status'],'FAILURE')
        self.assertEqual(result['reason'],'TARGET_NOT_REACHED_WITHIN_ORIGINAL_HORIZON')

    def test_same_daily_bar_touching_both_levels_is_ambiguous(self):
        result=daily_plan_outcome('2026-10-13',110,90,1,[self.bar('2026-10-14',high=112,low=88)])
        self.assertEqual(result['status'],'AMBIGUOUS')

    def test_opening_below_stop_precedes_a_later_target_touch_in_daily_proxy(self):
        result=daily_plan_outcome('2026-10-13',110,90,1,[self.bar('2026-10-14',op=85,high=112,low=80)])
        self.assertEqual(result['status'],'FAILURE')
        self.assertEqual(result['reason'],'OPEN_AT_OR_BELOW_INVALIDATION')

    def test_missing_required_session_is_not_silently_skipped(self):
        result=daily_plan_outcome('2026-10-13',110,90,2,[self.bar('2026-10-15',high=112)])
        self.assertEqual(result['status'],'UNAVAILABLE')

    def test_later_target_after_horizon_does_not_rescue_a_failed_plan(self):
        result=daily_plan_outcome('2026-10-13',110,90,1,
            [self.bar('2026-10-14'),self.bar('2026-10-15',high=120)])
        self.assertEqual(result['status'],'FAILURE')

    def test_a_next_day_price_cannot_fill_a_previous_rod_limit(self):
        candidate=dict(quote_timestamp='2026-10-16T11:00:00+08:00',quote_status='LIVE',ask=105)
        setup=dict(entry=100,invalidation=95)
        quotes=[dict(quote_timestamp='2026-10-19T09:20:00+08:00',quote_status='LIVE',ask=99,last_price=99)]
        self.assertEqual(find_quote_entry(candidate,setup,quotes)['status'],'UNCONFIRMED_ENTRY')

    def test_same_day_quote_can_support_a_limit_but_cannot_certify_an_actual_fill(self):
        candidate=dict(quote_timestamp='2026-10-16T11:00:00+08:00',quote_status='LIVE',ask=105)
        setup=dict(entry=100,invalidation=95)
        quotes=[dict(quote_timestamp='2026-10-16T12:00:00+08:00',quote_status='LIVE',ask=99.9,last_price=99.8)]
        result=find_quote_entry(candidate,setup,quotes)
        self.assertEqual(result['status'],'QUOTE_SUPPORTED')
        self.assertFalse(result['actual_fill_verified']);self.assertFalse(result['depth_verified'])
