"""The live dashboard keeps broker, P&L, and reason values in their columns."""
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from dashboard import Dashboard


class DashboardTradeDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.addCleanup(patch.stopall)
        patch('dashboard.ROOT', Path(self.temp.name)).start()
        patch.object(Dashboard, 'launch_health').start()
        with patch.object(Dashboard, 'refresh'):
            self.app = Dashboard(self.root, Path(self.temp.name), 120)
        patch.object(self.root, 'after').start()

    @staticmethod
    def row(tree):
        item = tree.get_children()[0]
        return dict(zip(tree['columns'], tree.item(item, 'values')))

    def test_broker_sellable_and_realized_values_follow_their_headings(self):
        self.app.last_health = {
            'broker_inventory_verified': True,
            'checked_at': '2026-10-21T09:30:00+08:00',
            'ai_positions': {'1101': 60},
            'inventory_comparison': [{
                'symbol': '1101', 'broker_cash_qty': 100,
                'broker_sellable_qty': 25, 'ai_qty': 60,
                'configured_user_qty': 40, 'status': 'MATCH',
            }],
            'execution_attempts': [{
                'decision_id': 'sell-1', 'started_at': '2026-10-21T09:10:00+08:00',
                'symbol': '1101', 'side': 'SELL', 'status': 'FILLED',
                'run_id': 'run-1',
                'detail': {'quantity': 5, 'filled_quantity': 5,
                           'limit_price': '101', 'seq13': 'SEQ-1'},
            }],
            'broker_order_reconciliation': [{
                'decision_id': 'sell-1', 'side': 'SELL',
                'broker_verified_filled_qty': 5,
                'broker_replay_fill_details': [{'quantity': 5, 'price_twd': '102'}],
            }],
        }
        with (patch.object(self.app, 'run_strategy_info', return_value=('策略 v1', '完整 AI 理由')),
              patch('dashboard.realized_price_difference',
                    return_value={'sell-1': (Decimal('0.49'), True)}),
              patch('dashboard.paper_portfolio.account', return_value=None)):
            self.app.render()

        position = self.row(self.app.position_tree)
        self.assertEqual(position['券商現股'], '100')
        self.assertEqual(position['券商可賣'], '25')
        self.assertEqual(position['AI 管理股數'], '60')
        broker = self.row(self.app.broker_position_tree)
        self.assertEqual(broker['券商可賣'], '25')
        self.assertEqual(broker['正式 AI 股數'], '60')
        trade = self.row(self.app.live_trade_tree)
        self.assertEqual(trade['已實現損益'], '約 0')
        self.assertEqual(trade['AI 理由'], '完整 AI 理由')
        self.assertEqual(trade['採用策略'], '策略 v1')
        self.assertEqual(trade['券商序號'], 'SEQ-1')

    def test_env_mode_is_selected_at_startup_and_visible_without_strategy(self):
        # Recreate the selector with an isolated state root and no inherited mode.
        Path(self.temp.name, '.env').write_text('TRADING_MODE=agent\n', encoding='utf-8')
        with patch.dict(os.environ):
            os.environ.pop('TRADING_MODE', None)
            second_root = tk.Tk()
            try:
                with (patch.object(Dashboard, 'refresh'),
                      patch.object(Dashboard, 'launch_health'),
                      patch('dashboard.strategy_catalog.live_selection',
                            side_effect=FileNotFoundError('missing strategy'))):
                    second = Dashboard(second_root, Path(self.temp.name), 120)
                self.assertEqual(second.decision_mode.get(), 'agent')
                self.assertIn('AGENT｜策略檔案無法讀取', second.live_strategy_text.get())
            finally:
                second_root.destroy()


if __name__ == '__main__':
    unittest.main()
