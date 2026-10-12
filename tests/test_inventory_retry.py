"""Retry only transient broker inventory reads; never replay an order."""
import unittest
from unittest.mock import Mock

from execution.account_guard import AccountCheckError, query_inventory
from execution.capital_reply_session import CapitalReplySessionError


class InventoryRetryTests(unittest.TestCase):
    def test_transient_1019_recovers_with_backoff(self):
        session = Mock()
        session.query_real_balance_rows.side_effect = [
            CapitalReplySessionError('GetRealBalanceReport failed: rc=1019'),
            CapitalReplySessionError('GetRealBalanceReport failed: rc=1019'),
            CapitalReplySessionError('GetRealBalanceReport failed: rc=1019'),
            [],
        ]
        self.assertEqual(query_inventory(session), [])
        self.assertEqual(session.query_real_balance_rows.call_count, 4)
        self.assertEqual([call.args[0] for call in session.pump.call_args_list],
                         [2.0, 4.0, 8.0])

    def test_persistent_1019_stops_after_four_read_attempts(self):
        session = Mock()
        session.query_real_balance_rows.side_effect = CapitalReplySessionError(
            'GetRealBalanceReport failed: rc=1019')
        with self.assertRaisesRegex(AccountCheckError, 'rc=1019'):
            query_inventory(session, retry_delay_seconds=0)
        self.assertEqual(session.query_real_balance_rows.call_count, 4)

    def test_other_error_is_not_retried(self):
        session = Mock()
        session.query_real_balance_rows.side_effect = CapitalReplySessionError(
            'GetRealBalanceReport failed: rc=1234')
        with self.assertRaisesRegex(AccountCheckError, 'rc=1234'):
            query_inventory(session)
        session.query_real_balance_rows.assert_called_once()
        session.pump.assert_not_called()


if __name__ == '__main__':
    unittest.main()
