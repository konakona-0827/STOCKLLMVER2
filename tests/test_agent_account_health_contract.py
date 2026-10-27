"""Exercise the real broker report -> saved snapshot -> Agent account boundary."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_decision import analyze_existing
from analysis_service import AnalysisService, live_buy_budget
from config import now
from execution.auto_advice_executor import LiveExecutionConfig
from execution.execution_store import ExecutionStore
from execution.health_monitor import persist_refresh_snapshot, current_buy_block_reasons
import paper_portfolio


class AgentAccountHealthContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = AnalysisService(self.root/'data/analysis')
        paper_portfolio.set_capital_limit(self.service.db_path, 10000)
        self.store = ExecutionStore(self.root/'data/execution/live_execution.sqlite3')
        self.store.bootstrap_ai_qty('2891', 25)
        self.store.bootstrap_ai_qty('1101', 100)
        self.health = dict(checked_at=now().isoformat(), status='OK', errors=[],
            inventory_checked=True, broker_balance_twd='25634',
            broker_one_account_balance_twd='25634', minimum_balance_twd='10000',
            strategy_cap_twd='10000', daily_buy_limit_twd='30000', daily_buy_reserved_twd='0',
            ai_committed_capital_twd='4566.5', pending_buy_reserve_twd='0',
            db_ai_positions={'2891': 25, '1101': 100}, broker_order_reconciliation=[],
            inventory_comparison=[dict(symbol=s, status='MATCH', ai_qty=q,
                broker_cash_qty=b, broker_sellable_qty=b, configured_user_qty=b-q)
                for s, q, b in [('2891', 25, 32), ('1101', 100, 100)]])

    def test_saved_health_preserves_verification_for_budget_and_sell_consumers(self):
        snapshot = persist_refresh_snapshot(self.root, self.health)
        self.assertTrue(snapshot.get('inventory_checked'))
        self.assertEqual(current_buy_block_reasons(snapshot), [])
        budget = live_buy_budget(snapshot, Decimal('0'), Decimal('10000'), Decimal('30000'))
        self.assertEqual(budget['available'], Decimal('5433.5'))

    def exercise_agent(self, inventory_verified):
        health = deepcopy(self.health)
        if not inventory_verified:
            health.update(status='ERROR', inventory_checked=False, errors=['inventory query failed'])
        runtime = SimpleNamespace(health=Mock(side_effect=lambda: deepcopy(health)))
        candidates = [dict(symbol=s, last_price=p, bid=p, ask=p, open=p, high=p+1, low=p-1,
            quote_status='LIVE', quote_timestamp=now().isoformat(), asset_type='STOCK',
            is_trial=False, paper_position=dict(qty=q), position=dict(ai_managed_qty=q),
            max_buy_qty=0, max_sell_qty=0, eligible_strategies=[])
            for s, q, p in [('2891', 25, 75), ('1101', 100, 26.5)]]
        observed = {}
        async def runner(context, *args):
            observed.update(deepcopy(context.get_portfolio()))
            context.get_trade_history()
            for c in candidates:
                context.get_market_data(c['symbol'])
            return dict(market_view='只驗證帳戶資料，不交易', decisions=[dict(
                symbol=c['symbol'], decision='HOLD', confidence=0, action_ratio=0,
                suggested_qty=0, suggested_price=None, reference_price=c['last_price'],
                strategy_id='NONE', target_price=None, invalidation_price=None, holding_days=None,
                reason='只驗證帳戶', data_quality='LIVE', warnings=[]) for c in candidates]), {}
        runtime.fetch_quotes = Mock(side_effect=lambda symbols, *_args, **_kwargs: ({
            c['symbol']: dict(symbol=c['symbol'], source='CAPITAL_SKCOM', name='測試',
                exchange_time=now().isoformat(), price=c['last_price'], bid=c['bid'], ask=c['ask'],
                open=c['open'], high=c['high'], low=c['low'], volume_shares=10000, is_trial=False)
            for c in candidates if c['symbol'] in symbols}, {}))
        account = dict(available_cash_cents=0, available_cash_twd=0, positions=[], capital_limit_twd=10000)
        with patch('agent_decision.ROOT', self.root), \
                patch('execution.broker_runtime.get_broker_runtime', return_value=runtime), \
                patch('quant_scanner.scan_quote', return_value={}), \
                patch('trading_agent.sdk_run', side_effect=runner):
            _, response = analyze_existing(self.service, self.root/'run',
                dict(run_id='account-contract', read_only=True),
                dict(model='test-model', instructions='只驗證帳戶', input='{}'),
                candidates, account, LiveExecutionConfig(), True, {})
        self.assertEqual(response['validation'], 'VALID', response)
        self.assertEqual({p['symbol']: p['qty'] for p in observed['positions']}, {'2891': 25, '1101': 100})
        expected = 5433.5 if inventory_verified else 0
        self.assertEqual(observed['available_cash_twd'], expected)
        self.assertEqual(observed['budget_status'], 'READY' if inventory_verified else 'BLOCKED')
        self.assertEqual({c['symbol']: c['max_sell_qty'] for c in candidates},
                         {'2891': 25, '1101': 100} if inventory_verified else {'2891': 0, '1101': 0})
        self.assertTrue(all(c['max_buy_qty'] > 0 for c in candidates) if inventory_verified
                        else all(c['max_buy_qty'] == 0 for c in candidates))

    def test_actual_sync_gives_agent_the_same_nonzero_budget_and_sell_limits_as_llm(self):
        self.exercise_agent(True)

    def test_failed_current_inventory_still_blocks_trades_without_erasing_holdings(self):
        self.exercise_agent(False)


if __name__ == '__main__':
    unittest.main()
