"""One reserve setting, independent capital ceiling and a smaller fresh budget."""
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analysis_service import live_buy_budget
from execution.auto_advice_executor import LiveExecutionConfig
from execution.execution_store import ExecutionStore
from execution.health_monitor import persist_refresh_snapshot
import paper_portfolio


class MinimumBalanceSettingTests(unittest.TestCase):
    def test_reserve_changes_both_directions_without_changing_other_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=root/'config.json';db=root/'data/analysis/market_history.sqlite3'
            original=dict(enabled=True,min_available_to_buy_twd=5000,max_daily_buy_twd=30000,
                          minimum_commission_twd=1,min_buy_fee_reserve_twd=20,planned_limits_enabled=False)
            cfg.write_text(json.dumps(original),encoding='utf-8-sig')
            paper_portfolio.set_capital_limit(db,10000)
            ExecutionStore(root/'data/execution/live_execution.sqlite3')
            for floor,expected in ((5000,'10000'),(10000,'6000'),(5000,'10000')):
                with self.subTest(floor=floor):
                    LiveExecutionConfig.set_minimum_balance(cfg,floor)
                    loaded=LiveExecutionConfig.load(cfg)
                    self.assertEqual(json.loads(cfg.read_text(encoding='utf-8')),{**original,'min_available_to_buy_twd':floor})
                    cap=Decimal(paper_portfolio.account(db)['capital_limit_cents'])/100
                    self.assertEqual(cap,10000)
                    report=dict(status='OK',checked_at='2026-11-16T10:00:00+08:00',errors=[],inventory_checked=True,
                        broker_balance_twd='16000',broker_one_account_balance_twd='3000',
                        minimum_balance_twd=str(loaded.min_available_to_buy_twd),strategy_cap_twd=str(cap),
                        pending_buy_reserve_twd='0',ai_committed_capital_twd='0',
                        daily_buy_limit_twd=str(loaded.max_daily_buy_twd),daily_buy_reserved_twd='0')
                    snapshot=persist_refresh_snapshot(root,report)
                    calculated=live_buy_budget(report,Decimal(0),cap,loaded.max_daily_buy_twd)
                    self.assertEqual(calculated['available'],Decimal(expected))
                    self.assertEqual(Decimal(snapshot['current_buy_budget_twd']),Decimal(expected))
                    self.assertEqual(snapshot['strategy_cap_twd'],'10000')

    def test_invalid_reserve_leaves_configuration_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json';path.write_text('{"min_available_to_buy_twd":5000}',encoding='utf-8')
            before=path.read_bytes()
            for invalid in (-1,1.5,True,'10000',None):
                with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                    LiveExecutionConfig.set_minimum_balance(path,invalid)
                self.assertEqual(path.read_bytes(),before)
            LiveExecutionConfig.set_minimum_balance(path,0)
            self.assertEqual(LiveExecutionConfig.load(path).min_available_to_buy_twd,0)

    def test_failed_atomic_replace_preserves_original_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json';path.write_text('{"min_available_to_buy_twd":5000}',encoding='utf-8')
            before=path.read_bytes()
            with patch('execution.auto_advice_executor.os.replace',side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):LiveExecutionConfig.set_minimum_balance(path,10000)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(list(path.parent.glob('*.tmp')),[])


if __name__=='__main__':
    unittest.main()
