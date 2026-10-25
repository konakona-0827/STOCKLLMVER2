"""Full Agent -> original advice -> real executor -> existing ledger, with only APIs mocked."""
from contextlib import closing
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from config import ROOT, now
from analysis_service import AnalysisService
from agent_decision import execution_history
from execution.execution_store import ExecutionStore
from execution.account_guard import BuyingPower, InventoryRow
from execution.account_snapshot import ExecutionAccountSnapshot
from execution.auto_advice_executor import execute_advice_file
from execution.capital_executor import SendResult
from execution.models import ExecutionStatus
import paper_portfolio
import strategy_catalog


class LiveAgentIntegrationTests(unittest.TestCase):
    def test_shared_current_holdings_buy_sell_hold_budget_recheck_and_mode_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'config').mkdir()
            for file in ('scan_rules.json','execution_live.json'):
                shutil.copyfile(ROOT/'config'/file,root/'config'/file)
            (root/'config'/'universe_tw.json').write_text(json.dumps(dict(symbols=['0050'],etf_symbols=['0050'])),encoding='utf-8')
            prompt = root/'strategy.md'
            prompt.write_text('既有策略',encoding='utf-8')
            service = AnalysisService(root/'data'/'analysis')
            paper_portfolio.set_capital_limit(service.db_path,100000)
            store = ExecutionStore(root/'data'/'execution'/'live_execution.sqlite3')
            store.bootstrap_ai_qty('0050',30)  # Current account, not a newly seeded Agent experiment.
            healthy = dict(status='OK',errors=[],broker_balance_twd='100000',broker_one_account_balance_twd='100000',
                           minimum_balance_twd='10000',inventory_checked=True,broker_inventory_verified=True,
                           pending_buy_reserve_twd='0',ai_committed_capital_twd='3000',
                           current_buy_budget_twd='30000',checked_at=now().isoformat())
            def quotes(symbols,*args,**kwargs):
                stamp = now().isoformat()
                return {s:dict(symbol=s,source='CAPITAL_SKCOM',name='真實介面測試ETF',exchange_time=stamp,
                               price=100,ask=100,bid=99.9,open=99,high=101,low=98,volume_shares=100000,is_trial=False)
                        for s in symbols},{}
            def health_snapshot(*args, **kwargs):
                fresh = {**healthy, 'inventory_comparison': [dict(
                    symbol='0050', status='MATCH', broker_sellable_qty=store.get_ai_qty('0050'))]}
                return (fresh, {}) if kwargs.get('return_report') else fresh
            power = BuyingPower(True,True,Decimal('100000'),Decimal('100000'),Decimal('100000'),'test')
            session = SimpleNamespace(connected=True,ensure_ready=Mock(return_value=SimpleNamespace(
                ready=True,reply_state=1,reply_connected=True,action='REUSE')))
            def execute(path):
                qty = store.get_ai_qty('0050')
                snapshot = ExecutionAccountSnapshot(now().isoformat(),power,
                    (InventoryRow('0050','T',qty,0,0,0,0,qty,qty,'test'),))
                return execute_advice_file(path,project_root=root,config_path=root/'config'/'execution_live.json',
                                          session=session,account_snapshot=snapshot,execution_db_path=store.path)
            runtime = SimpleNamespace(fetch_quotes=Mock(side_effect=quotes),execute=Mock(side_effect=execute))
            def send(**kwargs):
                kwargs['on_submitted'](SendResult(0,'test','1234567890123','test'))
                return SimpleNamespace(status=ExecutionStatus.FILLED,quantity=kwargs['quantity'],
                    price=kwargs['price'],send_return_code=0,seq13='1234567890123',filled_quantity=kwargs['quantity'],
                    broker_order_sent=True,send_attempted=True,crosscheck_performed=True,used_fallback=False,
                    symbol=kwargs['symbol'],side=kwargs['side'],changes_position=True,
                    crosscheck=SimpleNamespace(status=ExecutionStatus.FILLED,accepted_rows=[],fill_rows=[],cancel_rows=[],used_fallback=False))
            seen_quantities = []
            async def runner(context,*args):
                account = context.get_portfolio()
                seen_quantities.append(account['positions'][0]['qty'])
                context.get_trade_history()
                context.get_market_data('0050')
                held = account['positions'][0]['qty']
                decision = dict(symbol='0050',decision=action,confidence=0.8,
                    action_ratio=(5.5/held) if action=='SELL' else 0,
                    suggested_qty=5 if action in ('BUY','SELL') else 0,reference_price=100,
                    suggested_price=100 if action=='BUY' else 99.9 if action=='SELL' else None,
                    strategy_id='intraday_recovery' if action=='BUY' else 'NONE',
                    target_price=130 if action=='BUY' else None,invalidation_price=95 if action=='BUY' else None,
                    holding_days=10 if action=='BUY' else None,reason='Agent '+action,data_quality='LIVE',warnings=[])
                return dict(market_view='承接既有持倉',decisions=[decision]),dict(input_tokens=10,output_tokens=5)
            with patch.dict(os.environ,{'TRADING_MODE':'agent'}), patch('analysis_service.load_env'), \
                    patch('analysis_service.load_universe',return_value=dict(symbols=['0050'],etf_symbols=['0050'])), \
                    patch('analysis_service.ROOT',root), patch('agent_decision.ROOT',root), \
                    patch('strategy_catalog.live_selection',return_value=strategy_catalog.read_strategy(prompt)), \
                    patch('daily_history._fetch_month',return_value=([], 'OFFLINE_TEST')), \
                    patch('execution.broker_runtime.get_broker_runtime',return_value=runtime), \
                    patch('execution.health_monitor.sync_broker_state',side_effect=health_snapshot), \
                    patch('execution.health_monitor.latest_refresh_snapshot',return_value=healthy), \
                    patch('execution.health_monitor.run_health_check',return_value=healthy), \
                    patch('execution.health_monitor.persist_refresh_snapshot',return_value=healthy), \
                    patch('main.is_twse_oddlot_market_window',return_value=True), \
                    patch('analysis_service.twse_closed_reason',return_value=None), \
                    patch('trading_agent.sdk_run',side_effect=runner), \
                    patch('execution.auto_advice_executor.query_buying_power',return_value=power) as query, \
                    patch('execution.auto_advice_executor.CapitalOddLotExecutor.send_limit_order',side_effect=send) as sender:
                for action, expected in (('BUY',35),('HOLD',35),('SELL',30)):
                    result = service.run('MANUAL')
                    if action=='BUY':
                        first_advice = Path(result['manifest']['run_directory'])/'advice_interface.json'
                        self.assertEqual(result['execution_report']['agent_budget_check']['status'],'PASS')
                    self.assertEqual(result['manifest']['ai_validation'],'VALID',result)
                    self.assertEqual(store.get_ai_qty('0050'),expected,result.get('execution_report'))
                    self.assertEqual(result['execution_report']['trading_mode'],'agent')
                    payload = json.loads((Path(result['manifest']['run_directory'])/'advice_interface.json').read_text(encoding='utf-8'))
                    self.assertEqual(payload['trading_mode'],'agent')
                self.assertEqual(sender.call_count,2)
                self.assertEqual(seen_quantities,[30,35,35])
                action = 'BUY'
                query.return_value = BuyingPower(True,True,Decimal('100'),Decimal('100'),Decimal('100'),'fresh-low')
                failed = service.run('MANUAL')
                self.assertFalse(failed['manifest']['real_order_sent'])
                self.assertEqual(sender.call_count,2)
                self.assertEqual(store.get_ai_qty('0050'),30)
                with patch.dict(os.environ,{'TRADING_MODE':'llm'}), \
                        patch.object(service,'_run',return_value={'llm':True}):
                    self.assertEqual(service.run('MANUAL'),{'llm':True})
                self.assertEqual(store.get_ai_qty('0050'),30)
                query.return_value = power
                replay = execute(first_advice)
                self.assertEqual(replay['results'][0]['status'],'SKIPPED_DUPLICATE')
                self.assertEqual(sender.call_count,2)
                # The Python-computed total must not exceed remaining credit.
                invalid = json.loads(first_advice.read_text(encoding='utf-8'))
                invalid['run_id'] = 'agent-insufficient-budget'
                invalid['execution_budget']['available_cash_twd'] = 50
                invalid_path = root/'invalid.json'
                invalid_path.write_text(json.dumps(invalid),encoding='utf-8')
                rejected = execute(invalid_path)
                self.assertEqual(rejected['status'],'BLOCKED_BUDGET')
                self.assertEqual(sender.call_count,2)
            self.assertFalse((service.directory/'agent'/'experiments.sqlite3').exists())
            self.assertEqual(paper_portfolio.trades(service.db_path),[])
            self.assertEqual(len(execution_history(store.path)),3)  # Two fills + rejected fresh-budget BUY.


if __name__=='__main__':
    unittest.main()
