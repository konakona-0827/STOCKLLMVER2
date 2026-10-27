"""Exercise develop's data and guards through the actual Agent handoff."""
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_decision import analyze_existing, execution_history
from analysis_service import AnalysisService, apply_live_cost_basis
from config import ROOT, now
from execution.auto_advice_executor import LiveExecutionConfig
from execution.execution_store import ExecutionStore
from market_calendar import previous_trading_day
import paper_portfolio
from universe import load_rules


class AgentDevelopParityTests(unittest.TestCase):
    def test_trade_history_returns_saved_entry_plan_without_inventing_missing_plans(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = ExecutionStore(root/'execution.sqlite3')
            analysis = root/'analysis.sqlite3'
            import json
            with closing(sqlite3.connect(store.path)) as db:
                for number, run in enumerate(('with-plan', 'missing-plan')):
                    db.execute('INSERT INTO execution_attempts VALUES(?,?,?,?,?,?,?,?)',
                        (str(number),run,'2603','BUY',now().isoformat(),None,'FILLED',
                         json.dumps(dict(quantity=20,filled_quantity=20,limit_price='266.5'))))
                db.commit()
            original = dict(symbol='2603',decision='BUY',reason='Saved entry thesis',
                strategy_id='multi_day_trend',target_price=292.5,invalidation_price=254.5,holding_days=10)
            with closing(sqlite3.connect(analysis)) as db:
                db.execute('CREATE TABLE selection_analyses(run_id TEXT,decision_json TEXT)')
                db.execute('CREATE TABLE scan_runs(run_id TEXT,manifest_json TEXT)')
                db.execute('INSERT INTO selection_analyses VALUES(?,?)',
                    ('with-plan', json.dumps(dict(decisions=[original]))))
                db.execute('INSERT INTO scan_runs VALUES(?,?)',
                    ('with-plan', json.dumps(dict(strategy=dict(name='saved',version='v1')))))
                db.commit()
            rows = {row['run_id']:row for row in execution_history(store.path,analysis_path=analysis)}
            self.assertEqual(rows['with-plan']['original_decision']['invalidation_price'],254.5)
            self.assertEqual(rows['with-plan']['original_decision']['reason'],'Saved entry thesis')
            self.assertEqual(rows['with-plan']['original_decision']['source'],'SAVED_ANALYSIS_DECISION')
            self.assertIsNone(rows['missing-plan']['original_decision'])

    def test_latest_quote_preserves_daily_evidence_and_latest_account_context(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service = AnalysisService(root/'data'/'analysis')
            paper_portfolio.set_capital_limit(service.db_path, 10000)
            store = ExecutionStore(root/'data'/'execution'/'live_execution.sqlite3')
            store.bootstrap_ai_qty('0050', 100)
            with closing(sqlite3.connect(store.path)) as db:
                db.execute('''CREATE TABLE manual_ai_adoptions (
                    id INTEGER PRIMARY KEY, symbol TEXT, qty INTEGER,
                    unit_cost_twd TEXT, adopted_at TEXT)''')
                db.execute('INSERT INTO manual_ai_adoptions VALUES(1,?,?,?,?)',
                           ('0050', 100, '80', (now()-timedelta(days=7)).isoformat()))
                db.commit()
            days, cursor = [], now().date()
            for _ in range(5):
                cursor = previous_trading_day(cursor)
                days.append(dict(date=cursor.isoformat(), open=99, high=101,
                                 low=98, close=100, volume=10000,
                                 source='TWSE_DAILY', complete=True))
            candidate = dict(symbol='0050', last_price=100, bid=99.9, ask=100,
                quote_status='LIVE', quote_timestamp=now().isoformat(), is_trial=False,
                asset_type='ETF', paper_position=dict(qty=100), position=dict(ai_managed_qty=100),
                max_sell_qty=100, daily_bars=days,
                daily_history_status=dict(prior_complete_sessions=5), daily_trend=dict(ma5=100),
                market_history=[], eligible_strategies=['intraday_recovery', 'multi_day_trend'])
            candidate['price_levels'] = [dict(id='today_high', price=100.5, basis=now().date().isoformat())]
            health = dict(status='OK', errors=[], inventory_checked=True,
                broker_balance_twd='100000', broker_one_account_balance_twd='100000',
                minimum_balance_twd='0', pending_buy_reserve_twd='0', ai_committed_capital_twd='8000',
                inventory_comparison=[dict(symbol='0050', status='MATCH', broker_sellable_qty=0)],
                checked_at=now().isoformat())
            initial_health = deepcopy(health)
            initial_health['inventory_comparison'][0]['broker_sellable_qty'] = 100
            account = dict(available_cash_cents=1000000, available_cash_twd=10000,
                           capital_limit_twd=10000, positions=[])
            def quotes(*args, **kwargs):
                return {'0050': dict(symbol='0050', source='CAPITAL_SKCOM', name='測試ETF',
                    exchange_time=now().isoformat(), price=100, ask=100, bid=99.9,
                    open=99, high=101, low=98, volume_shares=100000, is_trial=False)}, {}
            async def runner(context, *args):
                fresh_account = context.get_portfolio()
                self.assertEqual(fresh_account['available_cash_twd'], 2000)
                self.assertEqual(fresh_account['positions'][0]['max_sell_qty'], 0)
                self.assertEqual(fresh_account['positions'][0]['ai_cost_basis']['average_unit_cost_twd'], '80')
                self.assertEqual(fresh_account['positions'][0]['ai_trade_dates']['records'][0]['date_basis'],
                                 'MANUAL_PURCHASE_ENTRY_DATE')
                context.get_trade_history()
                quote = context.get_market_data('0050')
                self.assertIn('multi_day_trend', quote['eligible_strategies'])
                self.assertEqual(quote['daily_history_status']['prior_complete_sessions'], 5)
                self.assertEqual(quote['daily_trend']['ma5'], 100)
                self.assertEqual(quote['max_buy_qty'], 19)
                self.assertEqual(next(row['price'] for row in quote['price_levels']
                                      if row['id'] == 'today_high'), 101)
                self.assertIn('high_5d', {row['id'] for row in quote['price_levels']})
                return dict(market_view='檢查可賣上限', decisions=[dict(
                    symbol='0050', decision='SELL', confidence=.8, action_ratio=1,
                    suggested_qty=100, reference_price=100, suggested_price=99.9,
                    strategy_id='NONE', target_price=None, invalidation_price=None, holding_days=None,
                    reason='測試最新可賣額', data_quality='LIVE', warnings=[])]), {}
            runtime = SimpleNamespace(fetch_quotes=Mock(side_effect=quotes))
            request = dict(model='test-model', instructions='測試策略', input='{}')
            with patch('agent_decision.ROOT', root), \
                    patch('main.is_twse_oddlot_market_window', return_value=True), \
                    patch('execution.broker_runtime.get_broker_runtime', return_value=runtime), \
                    patch('execution.health_monitor.sync_broker_state', return_value=(health, {})), \
                    patch('trading_agent.sdk_run', side_effect=runner):
                decision, response = analyze_existing(
                    service, root/'run', dict(run_id='parity'), request, [candidate], account,
                    LiveExecutionConfig(min_available_to_buy_twd=Decimal('0')), True,
                    load_rules(ROOT/'config'/'scan_rules.json'), broker_health=initial_health)
            self.assertEqual(response['validation'], 'VALID', response)
            self.assertEqual(decision['decisions'][0]['decision'], 'HOLD')
            self.assertIn('SELL_BLOCKED_BY_BROKER_SELLABLE_QTY', decision['decisions'][0]['warnings'])
            self.assertEqual(account['available_cash_twd'], 2000)
            self.assertEqual(initial_health['inventory_comparison'][0]['broker_sellable_qty'], 0)
            self.assertEqual(store.get_ai_qty('0050'), 100)

    def test_invalidated_cost_does_not_leave_a_stale_average(self):
        candidate = dict(symbol='0050', paper_position=dict(qty=10), position={})
        apply_live_cost_basis([candidate], {'0050': dict(
            quantity=10, average_unit_cost_twd='80', estimated=True)})
        self.assertEqual(candidate['position']['ai_average_cost'], 80)
        candidate['paper_position']['qty'] = 20
        apply_live_cost_basis([candidate], {'0050': dict(
            quantity=10, average_unit_cost_twd='80', estimated=True)})
        self.assertFalse(candidate['ai_cost_basis']['available'])
        self.assertNotIn('ai_average_cost', candidate['position'])


if __name__ == '__main__':
    unittest.main()
