"""Avoid redundant inventory queries after advice with no order."""
import unittest
from unittest.mock import patch

from analysis_service import broker_snapshot_after_execution


class BrokerSnapshotAfterExecutionTests(unittest.TestCase):
    def test_no_trade_reuses_verified_pre_execution_snapshot(self):
        previous = {'status': 'OK', 'broker_inventory_verified': True,
                    'checked_at': '2026-10-12T11:55:07+08:00'}
        with patch('execution.health_monitor.latest_refresh_snapshot', return_value=previous), \
                patch('execution.health_monitor.sync_broker_state') as refresh:
            self.assertIs(broker_snapshot_after_execution(
                {'status': 'NO_TRADE_ACTION'}, 'project'), previous)
        refresh.assert_not_called()

    def test_order_attempt_requires_new_broker_read(self):
        after = {'status': 'OK', 'checked_at': '2026-10-12T11:55:22+08:00'}
        with patch('execution.health_monitor.latest_refresh_snapshot') as previous, \
                patch('execution.health_monitor.sync_broker_state', return_value=after) as refresh:
            self.assertIs(broker_snapshot_after_execution(
                {'status': 'COMPLETE'}, 'project'), after)
        previous.assert_not_called()
        refresh.assert_called_once_with('project')

    def test_missing_pre_execution_snapshot_requires_new_read(self):
        with patch('execution.health_monitor.latest_refresh_snapshot', return_value=None), \
                patch('execution.health_monitor.sync_broker_state', return_value={'status': 'ERROR'}) as refresh:
            broker_snapshot_after_execution({'status': 'NO_TRADE_ACTION'}, 'project')
        refresh.assert_called_once_with('project')


if __name__ == '__main__':
    unittest.main()
