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
from execution.advice_adapter import build_advice_intents
from execution.execution_store import ExecutionStore
import paper_portfolio
import strategy_catalog
import scan_store
from test_opportunity_search import candidate


class OpportunityServiceTests(unittest.TestCase):
    def test_full_pool_is_scanned_shortlist_reaches_model_and_unchanged_scan_skips_model(self):
        self.exercise_service()

    def test_independent_scout_adds_an_outside_symbol_to_quotes_history_and_decision_pipeline(self):
        self.exercise_service(with_scout=True)

    def test_watch_only_candidate_reaches_real_service_model_gate_and_saved_diagnostics(self):
        self.exercise_service(with_scout=True,watch_only=True)

    def test_pipeline_exception_saves_failed_stage_instead_of_no_opportunity(self):
        with tempfile.TemporaryDirectory() as tmp:
            service=AnalysisService(Path(tmp)/'analysis')
            strategy=strategy_catalog.read_strategy(CODE_ROOT/'strategies/strategy_opportunity_v2.md')
            def failed(directory,manifest,phase,strategy):
                phase('DAILY_HISTORY')
                raise ValueError('synthetic history failure')
            with (patch.dict(os.environ,{'TRADING_MODE':'agent'}),patch('analysis_service.load_env'),
                  patch('strategy_catalog.live_selection',return_value=strategy),
                  patch.object(service,'_run',side_effect=failed)):
                with self.assertRaisesRegex(ValueError,'synthetic history failure'):service.run('AUTO')
            saved=next((service.directory/'runs').glob('*/opportunity_diagnostics.json'))
            diagnosis=json.loads(saved.read_text('utf-8'))
            self.assertEqual(diagnosis['outcome'],'PIPELINE_EXECUTION_FAILED')
            self.assertEqual(diagnosis['last_phase'],'DAILY_HISTORY')

    def test_scout_failure_keeps_a_valid_seed_buy_eligible_for_execution_checks(self):
        self.exercise_service(scout_unavailable=True)

    def test_unrelated_quote_failure_is_a_warning_and_valid_seed_buy_still_executes(self):
        self.exercise_service(scout_unavailable=True,scenario='partial_quote')

    def test_exhausted_budget_pauses_trades_without_an_error_and_recovers(self):
        self.exercise_service(scout_unavailable=True,scenario='empty_budget')

    def test_exhausted_budget_also_pauses_existing_holdings(self):
        self.exercise_service(scenario='empty_budget_holding')

    def test_pre_execution_budget_loss_pauses_all_trades_and_can_be_reevaluated(self):
        self.exercise_service(scout_unavailable=True,scenario='pre_execution_budget')

    def test_execution_error_does_not_cache_the_unhandled_decision(self):
        self.exercise_service(scout_unavailable=True,scenario='execution_error')

    def test_rejected_order_does_not_cache_the_unhandled_decision(self):
        self.exercise_service(scout_unavailable=True,scenario='execution_rejected')

    def test_failed_order_does_not_cache_the_unhandled_decision(self):
        self.exercise_service(scout_unavailable=True,scenario='execution_failed')

    def test_complete_quote_outage_never_becomes_actionable(self):
        self.exercise_service(scenario='quote_outage')

    def test_read_only_review_does_not_overwrite_live_review_state(self):
        self.exercise_service(scenario='read_only')

    def exercise_service(self,with_scout=False,watch_only=False,scout_unavailable=False,scenario=None):
        from opportunity_engine import policy
        search_policy=policy()
        if watch_only:search_policy['minimum_target_net_profit_twd']=10000
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
            if scout_unavailable:
                scout.update(status='UNAVAILABLE', error='TWSE_COMPANY_LIST_NOT_CURRENT')
            (root/'config/universe_tw.json').write_text(json.dumps(dict(symbols=symbols,etf_symbols=[])),encoding='utf-8')
            service=AnalysisService(root/'data/analysis')
            paper_portfolio.set_capital_limit(service.db_path,10000)
            store=ExecutionStore(root/'data/execution/live_execution.sqlite3')
            at=datetime(2026,11,3,10,30,tzinfo=TAIPEI)
            health=dict(status='OK',errors=[],broker_balance_twd='30000',broker_one_account_balance_twd='30000',
                minimum_balance_twd='10000',inventory_checked=True,broker_inventory_verified=True,
                pending_buy_reserve_twd='0',ai_committed_capital_twd='0',checked_at=at.isoformat(),
                inventory_comparison=[],broker_order_reconciliation=[])
            runtime=SimpleNamespace(fetch_quotes=Mock(return_value=({s:dict(symbol=s,source='CAPITAL_SKCOM',
                exchange_time=at.isoformat(),price=125,ask=125,bid=124.9,open=110 if s=='9999' else 123,high=126,low=110 if s=='9999' else 120,
                volume_shares=100000,is_trial=False) for s in quote_symbols},{})))
            if scenario == 'partial_quote':
                raw,errors=runtime.fetch_quotes.return_value
                raw.pop('0020');errors['0020']='ONE_SYMBOL_QUOTE_TIMEOUT'
            if scenario == 'quote_outage':
                runtime.fetch_quotes.return_value=({}, {s:'BROKER_QUOTES_UNAVAILABLE' for s in quote_symbols})
            if scenario in ('empty_budget','empty_budget_holding'):
                health['ai_committed_capital_twd']='10000'
            if scenario == 'empty_budget_holding':
                store.bootstrap_ai_qty('0001',10)
                health['inventory_comparison']=[dict(symbol='0001',broker_cash_qty=10,
                                                    broker_sellable_qty=10,status='MATCH')]
            def enrich(cs,*args,**kwargs):
                for c in cs:
                    base=candidate(c['symbol'])
                    for key in ('daily_bars','price_levels','eligible_strategies','strategy_evidence'):
                        c[key]=deepcopy(base[key])
                    c['daily_history_status']=dict(latest_date='2026-11-02',expected_latest_date='2026-11-02',
                        missing_20d_trading_dates=[],coverage='SUFFICIENT_FOR_20D_COMPARISON')
                return cs
            def model(service,directory,manifest,request,cs,account,*args,**kwargs):
                self.assertEqual(len(cs),1 if watch_only else 3)
                if watch_only:
                    self.assertTrue(cs[0]['research_only'])
                    self.assertEqual(cs[0]['trade_setups'],[])
                else:self.assertTrue(all(c['trade_setups'] for c in cs))
                if with_scout:
                    self.assertIn('9999',[c['symbol'] for c in cs])
                    self.assertTrue(next(c for c in cs if c['symbol']=='9999')['market_scout'])
                decisions = [dict(symbol=c['symbol'],decision='WAIT',
                    confidence=0,action_ratio=0,suggested_qty=0,reference_price=125,suggested_price=None,
                    strategy_id='NONE',target_price=None,invalidation_price=None,holding_days=None,
                    reason='測試等待事件確認',data_quality='LIVE',warnings=[]) for c in cs]
                if scout_unavailable:
                    setup = cs[0]['trade_setups'][0]
                    decisions[0].update(decision='BUY', confidence=0.6, suggested_qty=setup['max_qty'],
                        suggested_price=setup['entry'], strategy_id=setup['strategy_id'],
                        target_price=setup['target'], invalidation_price=setup['invalidation'],
                        holding_days=setup['holding_days'])
                return dict(market_view='測試研究結論',decisions=decisions),dict(
                        validation='VALID',api={},raw_response='')

            def execute(path):
                advice = json.loads(path.read_text(encoding='utf-8'))
                intents = build_advice_intents(advice)
                self.assertEqual(len(intents), int(scout_unavailable))
                if scout_unavailable:
                    self.assertEqual(intents[0].side.value, 'BUY')
                if runtime.execute.call_count == 1:
                    if scenario == 'execution_error':raise RuntimeError('SYNTHETIC_EXECUTION_FAILURE')
                    if scenario == 'execution_rejected':
                        return dict(status='COMPLETE',results=[dict(status='SKIPPED_CHECK_FAILED',broker_order_sent=False)])
                    if scenario == 'execution_failed':
                        return dict(status='COMPLETE',results=[dict(status='FAILED',broker_order_sent=False)])
                return dict(status='COMPLETE', results=[])

            runtime.execute = Mock(side_effect=execute)
            refreshed = dict(status='OK', current_buy_budget_twd='20000')
            if scenario == 'pre_execution_budget':refreshed['current_buy_budget_twd']='0'

            def sync(root, *, return_report=False):
                return (health, refreshed) if return_report else refreshed

            strategy=strategy_catalog.read_strategy(CODE_ROOT/'strategies'/
                ('strategy_opportunity_v2.md' if with_scout else 'strategy_opportunity_v1.md'))
            with (patch.dict(os.environ,{'TRADING_MODE':'agent'}),patch('analysis_service.load_env'),
                  patch('analysis_service.ROOT',root),patch('position_analyzer.ROOT',root),
                  patch('analysis_service.now',return_value=at),patch('capital_multi_quote.now',return_value=at),
                  patch('strategy_catalog.live_selection',return_value=strategy),
                  patch('opportunity_engine.policy',return_value=search_policy),
                  patch('market_discovery.discover',return_value=scout),
                  patch('daily_history.enrich_candidates',side_effect=enrich),
                  patch('execution.broker_runtime.get_broker_runtime',return_value=runtime),
                  patch('execution.health_monitor.sync_broker_state',side_effect=sync),
                  patch('agent_decision.analyze_existing',side_effect=model) as model_call):
                first=service.run('AUTO')
                review_path=service.directory/'opportunity_review_state.json'
                if scenario == 'quote_outage':
                    self.assertEqual(first['manifest']['status'],'ERROR')
                    self.assertIn('NO_VALID_MARKET_DATA',first['manifest']['error'])
                    model_call.assert_not_called();runtime.execute.assert_not_called()
                    return
                if scenario in ('empty_budget','empty_budget_holding','pre_execution_budget'):
                    self.assertEqual(first['manifest']['status'],'BLOCKED_BUDGET')
                    self.assertEqual(first['manifest']['error'],'')
                    self.assertEqual(first['execution_report']['status'],'BLOCKED_BUDGET')
                    self.assertEqual(first['execution_budget']['budget_status'],'BLOCKED')
                    saved=scan_store.saved_analysis(service.db_path,first['manifest']['run_id'])
                    self.assertEqual(saved['manifest']['status'],'BLOCKED_BUDGET')
                    self.assertEqual(saved['manifest']['execution_status'],'BLOCKED_BUDGET')
                    self.assertFalse(first['manifest']['real_order_sent'])
                    runtime.execute.assert_not_called()
                    state=json.loads(review_path.read_text('utf-8'))
                    self.assertFalse(state['cycle_complete'])
                    advice=json.loads((Path(first['manifest']['run_directory'])/'advice_interface.json').read_text('utf-8'))
                    self.assertEqual(advice['run_status'],'BLOCKED_BUDGET')
                    if scenario != 'pre_execution_budget':
                        model_call.assert_not_called()
                    if scenario == 'empty_budget_holding':return
                    health['ai_committed_capital_twd']='0'
                    refreshed['current_buy_budget_twd']='20000'
                    second=service.run('AUTO')
                    self.assertEqual(second['manifest']['status'],'COMPLETE')
                    self.assertEqual(second['manifest']['ai_validation'],'VALID')
                    self.assertEqual(second['opportunity_search']['trigger']['reason'],'PREVIOUS_CYCLE_NOT_HANDLED')
                    runtime.execute.assert_called_once()
                    self.assertTrue(json.loads(review_path.read_text('utf-8'))['cycle_complete'])
                    return
                if scenario in ('execution_error','execution_rejected','execution_failed'):
                    self.assertFalse(json.loads(review_path.read_text('utf-8'))['cycle_complete'])
                    second=service.run('AUTO')
                    self.assertEqual(second['opportunity_search']['trigger']['reason'],'PREVIOUS_CYCLE_NOT_HANDLED')
                    self.assertEqual(model_call.call_count,2)
                    self.assertEqual(runtime.execute.call_count,2)
                    self.assertNotEqual(runtime.execute.call_args_list[0],runtime.execute.call_args_list[1])
                    self.assertTrue(json.loads(review_path.read_text('utf-8'))['cycle_complete'])
                    return
                if scenario == 'read_only':
                    original=review_path.read_bytes()
                    probe=service.run('MANUAL',read_only=True)
                    self.assertEqual(probe['execution_report']['status'],'READ_ONLY')
                    self.assertEqual(review_path.read_bytes(),original)
                    runtime.execute.assert_called_once()
                    return
                if scenario == 'partial_quote':
                    self.assertEqual(first['manifest']['status'],'COMPLETE')
                    self.assertEqual(first['manifest']['quote_warnings'],{'0020':'ONE_SYMBOL_QUOTE_TIMEOUT'})
                self.assertEqual(first['opportunity_search']['search']['pool_count'],
                                 19 if scenario=='partial_quote' else 21 if with_scout else 20)
                self.assertEqual(len(first['analysis_set']),1 if watch_only else 3)
                diagnosis=first['opportunity_diagnostics']
                self.assertEqual(diagnosis['outcome'],
                    'BUY_DECISION_PRODUCED' if scout_unavailable else 'RESEARCH_COMPLETED_WITHOUT_BUY')
                saved=json.loads((Path(first['manifest']['run_directory'])/'opportunity_diagnostics.json').read_text('utf-8'))
                self.assertEqual(saved,diagnosis)
                if watch_only:
                    self.assertEqual(diagnosis['counts']['watch_research'],1)
                    self.assertEqual(diagnosis['counts']['qualified_entries'],0)
                self.assertEqual(len(first['top']),10)
                second=service.run('AUTO')
                self.assertEqual(model_call.call_count,1)
                self.assertEqual(second['manifest']['ai_validation'],'NOT_CALLED')
                self.assertEqual(second['opportunity_search']['trigger']['reason'],'UNCHANGED_OPPORTUNITIES_AND_HOLDINGS')
                runtime.execute.assert_called_once()
                if scout_unavailable:
                    self.assertEqual(first['manifest']['status'], 'COMPLETE')
                    self.assertEqual(first['manifest']['research_warning'], scout['error'])
                    self.assertEqual(first['execution_report']['status'], 'COMPLETE')
                    self.assertFalse(first['manifest']['real_order_sent'])
                if with_scout:
                    self.assertIn('9999',runtime.fetch_quotes.call_args[0][0])
                    self.assertEqual(first['market_discovery']['watch_status'][0]['status'],'RESEARCH_CONDITION_MET')


if __name__=='__main__':unittest.main()
