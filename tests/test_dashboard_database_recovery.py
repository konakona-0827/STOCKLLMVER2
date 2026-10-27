"""Branch switches may remove JSON exports; the account's database must remain readable."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from analysis_service import AnalysisService
from dashboard import Dashboard
import paper_portfolio
import scan_store


class DashboardDatabaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.service = AnalysisService(self.folder)
        paper_portfolio.set_capital_limit(self.service.db_path, 10000)
        with closing(sqlite3.connect(self.service.db_path)) as db:
            scan_store.initialize(db)
            db.execute('INSERT INTO scan_runs VALUES(?,?)', ('run-1', json.dumps(dict(run_id='run-1',
                strategy=dict(name='original-strategy',version='123'), symbols_received=1))))
            db.execute('INSERT INTO selection_analyses VALUES(?,?,?,?)', ('run-1',
                json.dumps(dict(input=json.dumps(dict(analysis_set=[dict(symbol='2330')])))),
                '{}', json.dumps(dict(decisions=[dict(symbol='2330',decision='BUY',reason='saved reason')]))))
            db.execute('INSERT INTO multi_market_snapshots(run_id,symbol,snapshot_json) VALUES(?,?,?)',
                ('run-1','2330',json.dumps(dict(symbol='2330',last_price=100,quote_status='LIVE'))))
            db.commit()

    def test_missing_or_corrupt_export_recovers_history_without_restoring_order_authority(self):
        for damaged in (False, True):
            if damaged:
                (self.folder/'latest_dashboard.json').write_text('{', encoding='utf-8')
            with patch('execution.health_monitor.latest_refresh_snapshot', return_value=None):
                result, positions, broker = self.service.refresh()
            self.assertTrue(result['historical_view_only'])
            self.assertEqual(result['decision']['decisions'][0]['reason'], 'saved reason')
            self.assertIn('非本輪行情', result['manifest']['display_notice'])
            self.assertEqual(result['paper_simulation']['capital_limit_twd'], 10000)
            self.assertIsNone(broker)
            self.assertNotIn('execution_report', result)
            self.assertFalse((self.folder/'latest_advice.json').exists())
        with closing(sqlite3.connect(self.service.db_path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM scan_runs').fetchone()[0], 1)

    def test_strategy_and_reason_display_use_database_when_run_files_are_missing(self):
        app = Dashboard.__new__(Dashboard)
        app.service = self.service
        app.strategy_audit_cache = {}
        self.assertEqual(app.run_strategy_info('run-1','2330'), ('original-strategy v123','saved reason'))

    def test_database_without_analyses_has_no_historical_result(self):
        with closing(sqlite3.connect(self.service.db_path)) as db:
            db.execute('DELETE FROM selection_analyses')
            db.commit()
        self.assertIsNone(scan_store.historical_dashboard(self.service.db_path))


if __name__ == '__main__':
    unittest.main()
