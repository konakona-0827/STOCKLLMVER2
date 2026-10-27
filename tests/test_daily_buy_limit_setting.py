from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from analysis_service import live_buy_budget
from execution.auto_advice_executor import LiveExecutionConfig


class DailyBuyLimitSettingTests(unittest.TestCase):
    def test_changed_limit_persists_and_changes_remaining_buy_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / 'execution_live.json'
            config_path.write_text(json.dumps({
                'enabled': True, 'max_daily_buy_twd': 20000,
                'min_available_to_buy_twd': 10000,
            }), encoding='utf-8')
            health = dict(status='OK', errors=[], inventory_checked=True,
                          broker_balance_twd='100000',
                          broker_one_account_balance_twd='100000',
                          minimum_balance_twd='10000',
                          pending_buy_reserve_twd='0',
                          ai_committed_capital_twd='0')

            for amount, expected in ((30000, Decimal('25000')),
                                     (4000, Decimal('0'))):
                LiveExecutionConfig.set_daily_buy_limit(config_path, amount)
                config = LiveExecutionConfig.load(config_path)
                result = live_buy_budget(health, Decimal('5000'),
                                         Decimal('60000'), config.max_daily_buy_twd)
                self.assertEqual(Decimal(amount), config.max_daily_buy_twd)
                self.assertEqual(expected, result['available'])
                self.assertTrue(json.loads(config_path.read_text(encoding='utf-8'))['enabled'])

    def test_rejects_nonpositive_limit_without_changing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / 'execution_live.json'
            config_path.write_text('{"max_daily_buy_twd": 20000}', encoding='utf-8')
            with self.assertRaises(ValueError):
                LiveExecutionConfig.set_daily_buy_limit(config_path, 0)
            self.assertEqual(Decimal('20000'),
                             LiveExecutionConfig.load(config_path).max_daily_buy_twd)


if __name__ == '__main__':
    unittest.main()
