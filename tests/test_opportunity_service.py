from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from analysis_service import AnalysisService
from config import CODE_ROOT,TAIPEI
from execution.execution_store import ExecutionStore
import paper_portfolio
import strategy_catalog
from test_opportunity_search import candidate


class OpportunityServiceTests(unittest.TestCase):
    def test_full_pool_is_scanned_shortlist_reaches_model_and_unchanged_scan_skips_model(self):
        self.exercise_service()

    def test_independent_scout_adds_an_outside_symbol_to_quotes_history_and_decision_pipeline(self):
        self.exercise_service(with_scout=True)

    def exercise_service(self,with_scout=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'config').mkdir()
            for name in ('execution_live.json','scan_rules.json'):
                shutil.copyfile(CODE_ROOT/'config'/name,root/'config'/name)
            symbols=[f'{i:04}' for i in range(1,21)]
            quote_symbols=symbols+(['9999'] if with_scout else [])
            scout=dict(status='COMPLETE' if with_scout else 'TEST_DISABLED',symbols=['9999'] if with_scout else [],
                watchlist=[dict(symbol='9999',trigger_kind='BREAK_ABOVE_HIGH',condition_price=120,
                    rationale='研究新標的',counter_evidence_to_check='核對阻力',expires_session='2026-11-03')] if with_scout else [],
                model_called=False)
            (root/'config/universe_tw.json').write_text(json.dumps(dict(symbols=symbols,etf_symbols=[])),encoding='utf-8')
            service=AnalysisService(root/'data/analysis')
            paper_portfolio.set_capital_limit(service.db_path,10000)
            ExecutionStore(root/'data/execution/live_execution.sqlite3')
            at=datetime(2026,11,3,10,30,tzinfo=TAIPEI)
            health=dict(status='OK',errors=[],broker_balance_twd='30000',broker_one_account_balance_twd='30000',
                minimum_balance_twd='10000',inventory_checked=True,broker_inventory_verified=True,
                pending_buy_reserve_twd='0',ai_committed_capital_twd='0',checked_at=at.isoformat(),
                inventory_comparison=[],broker_order_reconciliation=[])
            runtime=SimpleNamespace(fetch_quotes=Mock(return_value=({s:dict(symbol=s,source='CAPITAL_SKCOM',
                exchange_time=at.isoformat(),price=125,ask=125,bid=124.9,open=110 if s=='9999' else 123,high=126,low=110 if s=='9999' else 120,
                volume_shares=100000,is_trial=False) for s in quote_symbols},{})))
            def enrich(cs,*args,**kwargs):
                for c in cs:
                    base=candidate(c['symbol'])
                    for key in ('daily_bars','price_levels','eligible_strategies','strategy_evidence'):
                        c[key]=deepcopy(base[key])
                    c['daily_history_status']=dict(latest_date='2026-11-02',expected_latest_date='2026-11-02',
                        missing_20d_trading_dates=[],coverage='SUFFICIENT_FOR_20D_COMPARISON')
                return cs
            def model(service,directory,manifest,request,cs,account,*args,**kwargs):
                self.assertEqual(len(cs),3)
                self.assertTrue(all(c['trade_setups'] for c in cs))
                if with_scout:
                    self.assertIn('9999',[c['symbol'] for c in cs])
                    self.assertTrue(next(c for c in cs if c['symbol']=='9999')['market_scout'])
                return dict(market_view='測試研究結論',decisions=[dict(symbol=c['symbol'],decision='WAIT',
                    confidence=0,action_ratio=0,suggested_qty=0,reference_price=125,suggested_price=None,
                    strategy_id='NONE',target_price=None,invalidation_price=None,holding_days=None,
                    reason='測試等待事件確認',data_quality='LIVE',warnings=[]) for c in cs]),dict(
                        validation='VALID',api={},raw_response='')
            strategy=strategy_catalog.read_strategy(CODE_ROOT/'strategies'/
                ('strategy_opportunity_v2.md' if with_scout else 'strategy_opportunity_v1.md'))
            with (patch.dict(os.environ,{'TRADING_MODE':'agent'}),patch('analysis_service.load_env'),
                  patch('analysis_service.ROOT',root),patch('position_analyzer.ROOT',root),
                  patch('analysis_service.now',return_value=at),patch('capital_multi_quote.now',return_value=at),
                  patch('strategy_catalog.live_selection',return_value=strategy),
                  patch('market_discovery.discover',return_value=scout),
                  patch('daily_history.enrich_candidates',side_effect=enrich),
                  patch('execution.broker_runtime.get_broker_runtime',return_value=runtime),
                  patch('execution.health_monitor.sync_broker_state',return_value=(health,{})),
                  patch('agent_decision.analyze_existing',side_effect=model) as model_call,
                  patch.object(service,'_execute_after_advice_written',return_value=None)):
                first=service.run('AUTO')
                self.assertEqual(first['opportunity_search']['search']['pool_count'],21 if with_scout else 20)
                self.assertEqual(len(first['analysis_set']),3)
                self.assertEqual(len(first['top']),10)
                second=service.run('AUTO')
                self.assertEqual(model_call.call_count,1)
                self.assertEqual(second['manifest']['ai_validation'],'NOT_CALLED')
                self.assertEqual(second['opportunity_search']['trigger']['reason'],'UNCHANGED_OPPORTUNITIES_AND_HOLDINGS')
                if with_scout:
                    self.assertIn('9999',runtime.fetch_quotes.call_args[0][0])
                    self.assertEqual(first['market_discovery']['watch_status'][0]['status'],'RESEARCH_CONDITION_MET')


if __name__=='__main__':unittest.main()
