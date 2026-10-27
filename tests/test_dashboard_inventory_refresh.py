"""Transient broker inventory errors trigger a bounded GUI refresh."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from dashboard import Dashboard, inventory_temporarily_busy


BUSY = {'inventory_checked': False,
        'errors': ['inventory: AccountCheckError: GetRealBalanceReport failed: rc=1019']}


class DashboardInventoryRefreshTests(unittest.TestCase):
    def make_dashboard(self):
        dashboard = Dashboard.__new__(Dashboard)
        callbacks = []
        dashboard.root = SimpleNamespace(after=lambda delay, callback: callbacks.append((delay, callback)))
        dashboard.scheduler = SimpleNamespace(scan_lock=SimpleNamespace(locked=lambda: False))
        dashboard.refresh = Mock()
        dashboard.closing = False
        dashboard.refresh_pending = False
        dashboard.last_health = BUSY
        dashboard.inventory_retry_attempts = 0
        dashboard.inventory_retry_scheduled = False
        return dashboard, callbacks

    def test_1019_schedules_bounded_broker_refresh(self):
        dashboard, callbacks = self.make_dashboard()
        self.assertTrue(inventory_temporarily_busy(BUSY))
        dashboard.schedule_inventory_refresh()
        dashboard.schedule_inventory_refresh()
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0][0], 12000)
        # The refresh worker reports another 1019 on each attempted read.
        for attempt in range(1, 4):
            if attempt > 1:
                dashboard.schedule_inventory_refresh()
            callbacks.pop(0)[1]()
            self.assertEqual(dashboard.inventory_retry_attempts, attempt)
        self.assertEqual(dashboard.refresh.call_count, 3)
        dashboard.refresh.assert_called_with(sync_broker=True)
        dashboard.schedule_inventory_refresh()
        self.assertEqual(callbacks, [])

    def test_success_or_other_error_cancels_pending_retry(self):
        dashboard, callbacks = self.make_dashboard()
        dashboard.schedule_inventory_refresh()
        dashboard.last_health = {'inventory_checked': True, 'errors': []}
        callbacks.pop(0)[1]()
        dashboard.refresh.assert_not_called()
        self.assertFalse(inventory_temporarily_busy(
            {'inventory_checked': False, 'errors': ['inventory: rc=1234']}))


if __name__ == '__main__':
    unittest.main()
