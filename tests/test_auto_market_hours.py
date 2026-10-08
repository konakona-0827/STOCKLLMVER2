from __future__ import annotations

import unittest
from datetime import datetime

from analysis_service import should_call_openai
from config import TAIPEI
from scheduler import (
    Scheduler, is_twse_oddlot_market_window, is_health_check_window,
    health_check_due_or_next, next_health_check_after,
)


def wall(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=TAIPEI).timestamp()


class AutoMarketHoursTests(unittest.TestCase):
    def test_waiting_before_open_starts_at_nine_oh_five_taipei_time(self):
        scheduler = Scheduler(1800, allowed_slot=is_twse_oddlot_market_window,
                              phase_offset_seconds=300)
        scheduler.start_auto(wall(2026, 10, 8, 8, 15))
        self.assertEqual(wall(2026, 10, 8, 9, 5), scheduler.next_auto)
        self.assertFalse(scheduler.tick(wall(2026, 10, 8, 9, 0)))
        self.assertTrue(scheduler.tick(wall(2026, 10, 8, 9, 5)))
        scheduler.finish()
        self.assertEqual(wall(2026, 10, 8, 9, 35), scheduler.next_auto)

    def test_after_close_waits_for_next_weekday_open(self):
        scheduler = Scheduler(1800, allowed_slot=is_twse_oddlot_market_window,
                              phase_offset_seconds=300)
        scheduler.start_auto(wall(2026, 10, 16, 13, 45))
        self.assertEqual(wall(2026, 10, 19, 9, 5), scheduler.next_auto)

    def test_last_slot_is_thirteen_oh_five(self):
        scheduler = Scheduler(1800, allowed_slot=is_twse_oddlot_market_window,
                              phase_offset_seconds=300)
        scheduler.start_auto(wall(2026, 10, 8, 12, 50))
        self.assertEqual(wall(2026, 10, 8, 13, 5), scheduler.next_auto)
        self.assertTrue(scheduler.tick(wall(2026, 10, 8, 13, 5)))
        scheduler.finish()
        self.assertEqual(wall(2026, 10, 9, 9, 5), scheduler.next_auto)

    def test_auto_openai_requires_open_window_and_live_nontrial_quote(self):
        live = [{"quote_status": "LIVE", "is_trial": False}]
        stale = [{"quote_status": "LAST_KNOWN", "is_trial": False}]
        trial = [{"quote_status": "LIVE", "is_trial": True}]
        before_open = wall(2026, 10, 8, 8, 59)
        during_market = wall(2026, 10, 8, 9, 0)

        self.assertFalse(should_call_openai("AUTO", live, False, before_open))
        self.assertTrue(should_call_openai("AUTO", live, False, during_market))
        self.assertFalse(should_call_openai("AUTO", stale, False, during_market))
        self.assertFalse(should_call_openai("AUTO", trial, False, during_market))
        self.assertFalse(should_call_openai("AUTO", live, True, during_market))
        self.assertTrue(should_call_openai("MANUAL", live, False, before_open))

    def test_health_checks_are_limited_to_eight_to_fourteen_taipei(self):
        self.assertFalse(is_health_check_window(wall(2026, 10, 8, 7, 59)))
        self.assertTrue(is_health_check_window(wall(2026, 10, 8, 8, 0)))
        self.assertTrue(is_health_check_window(wall(2026, 10, 8, 14, 0)))
        self.assertFalse(is_health_check_window(wall(2026, 10, 8, 14, 1)))
        self.assertFalse(is_health_check_window(wall(2026, 10, 10, 10, 0)))

        self.assertEqual(
            wall(2026, 10, 8, 8, 0),
            health_check_due_or_next(wall(2026, 10, 8, 7, 30)),
        )
        self.assertEqual(
            wall(2026, 10, 8, 9, 0),
            next_health_check_after(wall(2026, 10, 8, 8, 15)),
        )
        self.assertEqual(
            wall(2026, 10, 9, 8, 0),
            next_health_check_after(wall(2026, 10, 8, 14, 0)),
        )
        self.assertEqual(
            wall(2026, 10, 12, 8, 0),
            health_check_due_or_next(wall(2026, 10, 10, 15, 0)),
        )


if __name__ == "__main__":
    unittest.main()
