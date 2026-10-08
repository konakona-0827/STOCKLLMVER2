from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from execution.execution_store import ExecutionStore
from execution.health_monitor import latest_refresh_snapshot, persist_refresh_snapshot


class BrokerRefreshSnapshotTests(unittest.TestCase):
    def test_refresh_persists_broker_evidence_and_formal_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / 'data' / 'execution' / 'live_execution.sqlite3'
            store = ExecutionStore(db_path)
            self.assertTrue(store.claim('run-1:2345', 'run-1', '2345', 'BUY'))
            store.finish('run-1:2345', 'SKIPPED_CHECK_FAILED', {'reason': 'no fresh quote'})
            report = dict(checked_at='2026-10-08T11:00:00+08:00', status='OK',
                          broker_balance_twd='30541', minimum_balance_twd='10000',
                          daily_buy_limit_twd='20000', daily_buy_reserved_twd='12000',
                          strategy_cap_twd='10000',
                          balance_below_floor=False, inventory_checked=True,
                          inventory_comparison=[dict(symbol='2449', broker_cash_qty=3,
                                                     ai_qty=0, configured_user_qty=3,
                                                     status='MATCH')],
                          db_ai_positions={}, unresolved_attempts=[], errors=[])
            snapshot = persist_refresh_snapshot(root, report)
            self.assertEqual('30541', snapshot['broker_balance_twd'])
            self.assertTrue(snapshot['broker_inventory_verified'])
            self.assertEqual('8000.0', snapshot['current_buy_budget_twd'])
            self.assertEqual('READY', snapshot['budget_status'])
            self.assertEqual('SKIPPED_CHECK_FAILED', snapshot['execution_attempts'][0]['status'])
            self.assertEqual(snapshot, latest_refresh_snapshot(root))
            with closing(sqlite3.connect(db_path)) as db:
                self.assertEqual(1, db.execute('SELECT COUNT(*) FROM broker_refresh_snapshots').fetchone()[0])
                self.assertEqual(0, db.execute('SELECT COUNT(*) FROM ai_positions').fetchone()[0])

    def test_incomplete_broker_query_is_not_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ExecutionStore(root / 'data' / 'execution' / 'live_execution.sqlite3')
            snapshot = persist_refresh_snapshot(root, dict(
                checked_at='2026-10-08T11:00:00+08:00', status='ERROR',
                broker_balance_twd=None, inventory_checked=False,
                errors=['session unavailable']))
            self.assertFalse(snapshot['broker_inventory_verified'])


if __name__ == '__main__':
    unittest.main()
