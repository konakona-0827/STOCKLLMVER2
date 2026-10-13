import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import Dashboard


class RefreshFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.addCleanup(patch.stopall)
        patch('dashboard.ROOT', Path(self.tmp.name)).start()
        patch.object(Dashboard, 'update_live_strategy_label').start()
        patch.object(Dashboard, 'launch_health').start()
        with patch.object(Dashboard, 'refresh'):
            self.app = Dashboard(self.root, Path(self.tmp.name), 120)
        self.app.next_health_at = time.time() + 3600
        patch.object(self.root, 'after').start()
        patch.object(self.app, 'render').start()
        self.worker = patch('dashboard.threading.Thread').start()
        self.service = patch.object(self.app.service, 'refresh').start()
        # Any accidental real broker access must fail this isolated GUI test.
        patch('execution.health_monitor.sync_broker_state',
              side_effect=AssertionError('Unexpected broker access')).start()

    def finish(self, snapshot=None, error=None):
        self.service.side_effect = error
        self.service.return_value = (None, [], snapshot)
        self.worker.call_args.kwargs['target']()
        self.app.tick()
        self.assertFalse(self.app.refresh_pending)
        self.assertFalse(self.app.refresh_button.instate(['disabled']))
        self.assertEqual(0, self.app.refresh_progress['value'])
        return self.app.refresh_text.get()

    def test_button_shows_activity_elapsed_time_and_success(self):
        self.app.refresh_button.invoke()
        self.assertTrue(self.app.refresh_pending)
        self.assertTrue(self.app.refresh_button.instate(['disabled']))
        self.assertIn('正在查詢券商', self.app.refresh_text.get())
        self.app.refresh_started_at -= 5
        self.app.tick()
        self.assertIn('已等待 5 秒', self.app.refresh_text.get())
        self.app.refresh(sync_broker=True)
        self.assertEqual(1, self.worker.call_count)
        text = self.finish(dict(status='OK', errors=[], broker_inventory_verified=True,
                                checked_at='2026-10-13T18:20:20+08:00'))
        self.service.assert_called_once_with(sync_broker=True)
        self.assertIn('重新整理與券商對帳完成', text)
        self.assertIn('2026-10-13T18:20:20+08:00', text)
        self.assertIn('耗時 5 秒', text)

    def test_returned_broker_error_is_failure_and_retry_can_succeed(self):
        self.app.refresh_button.invoke()
        with patch.dict('os.environ', {'CAPITAL_PASSWORD': 'test-secret'}):
            text = self.finish(dict(status='ERROR', broker_inventory_verified=False,
                                    errors=['DLL missing test-secret']))
        self.assertIn('券商對帳失敗', text)
        self.assertIn('DLL missing', text)
        self.assertNotIn('test-secret', text)
        self.assertNotIn('對帳完成', text)
        self.app.refresh_button.invoke()
        text = self.finish(dict(status='OK', errors=[], broker_inventory_verified=True))
        self.assertIn('對帳完成', text)
        self.assertNotIn('DLL missing', text)

    def test_exception_releases_button_and_shows_error(self):
        self.app.refresh_button.invoke()
        text = self.finish(error=RuntimeError('connection timed out'))
        self.assertIn('重新整理失敗', text)
        self.assertIn('connection timed out', text)

    def test_incomplete_inventory_and_partial_quote_failure_are_visible(self):
        self.app.refresh_button.invoke()
        text = self.finish(dict(status='WARN', errors=[], broker_inventory_verified=False))
        self.assertIn('尚未完成核對', text)
        self.assertNotIn('對帳完成', text)
        self.app.refresh_button.invoke()
        text = self.finish(dict(status='OK', errors=[], broker_inventory_verified=True,
                                position_quote_errors={'0050': 'quote timeout'}))
        self.assertIn('對帳完成', text)
        self.assertIn('部分持股行情更新失敗', text)
        self.assertIn('quote timeout', text)

    def test_busy_scan_is_not_silently_ignored(self):
        self.app.scheduler.scan_lock.acquire()
        try:
            self.app.refresh_button.invoke()
            self.assertIn('尚未開始重新整理', self.app.refresh_text.get())
            self.worker.assert_not_called()
        finally:
            self.app.scheduler.scan_lock.release()
        self.app.refresh_button.invoke()
        self.app.scheduler.scan_lock.acquire()
        try:
            self.assertIn('本次資料尚未套用', self.finish())
        finally:
            self.app.scheduler.scan_lock.release()

    def test_local_refresh_does_not_claim_broker_was_queried(self):
        self.app.refresh()
        self.assertIn('正在讀取本機資料', self.app.refresh_text.get())
        text = self.finish()
        self.assertIn('本機資料已更新', text)
        self.assertNotIn('對帳完成', text)
        self.service.assert_called_once_with(sync_broker=False)


if __name__ == '__main__':
    unittest.main()
