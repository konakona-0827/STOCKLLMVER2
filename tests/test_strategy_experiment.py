import tempfile
import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import patch

from config import ROOT, now
from execution.auto_advice_executor import LiveExecutionConfig
from strategy_experiment import ExperimentStore, ExperimentService, fixed_rule_decision
import position_analyzer


class StrategyExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.store = ExperimentStore(self.folder/'experiment.sqlite3')
        self.store.configure(100000, 2, 8)
        self.prompt = self.folder/'version.md'
        self.prompt.write_text('第一版策略',encoding='utf-8')
        self.first = self.store.add_strategy(self.prompt)
        self.rules = LiveExecutionConfig.load(ROOT/'config'/'execution_live.json')

    def tearDown(self):
        self.tmp.cleanup()

    def candidate(self, stamp=None):
        return dict(symbol='0050',name='元大台灣50',quote_status='LIVE',
                    quote_timestamp=stamp or now().isoformat(),is_trial=False,
                    last_price=100,ask=100,bid=99.9,open=99,high=101,low=98,
                    volume=10000,eligible_strategies=['intraday_recovery'],quant_score=10,
                    analysis_source='TOP10',max_buy_qty=999,
                    paper_position=dict(qty=0,cost_cents=0),
                    position=dict(ai_managed_qty=0))

    def decision(self, side='BUY', qty=10):
        return dict(market_view='測試',decisions=[dict(symbol='0050',decision=side,
                    suggested_qty=qty,reason='依據策略買進',action_ratio=0,
                    confidence=1,reference_price=100,suggested_price=100,
                    strategy_id='intraday_recovery' if side=='BUY' else 'NONE',
                    target_price=120 if side=='BUY' else None,
                    invalidation_price=95 if side=='BUY' else None,
                    holding_days=10 if side=='BUY' else None,
                    data_quality='LIVE',warnings=['預算'])])

    def test_versioned_accounts_pause_and_forward_start(self):
        old = self.store.strategies()[0]
        self.prompt.write_text('第二版策略',encoding='utf-8')
        second = self.store.add_strategy(self.prompt)
        self.assertNotEqual(old['version'],self.store.strategies()[1]['version'])
        self.assertEqual(self.store.strategies()[0]['prompt'],'第一版策略')
        self.store.set_active(self.first,False)
        self.store.set_active(self.first,True)
        self.assertEqual(self.store.summary(self.first)['cash_twd'],100000)
        self.assertEqual(self.store.summary(second)['trade_count'],0)
        self.store.configure(200000)
        self.assertEqual(self.store.summary(self.first)['capital_limit_twd'],100000)
        self.assertEqual(self.store.summary(second)['capital_limit_twd'],100000)
        self.assertEqual(self.store.settings()['initial_cents'],20000000)

    def test_each_strategy_has_own_capital_and_increase_is_not_profit(self):
        second_prompt = self.folder/'second.md'
        second_prompt.write_text('另一策略',encoding='utf-8')
        second = self.store.add_strategy(second_prompt,initial_twd=50000)
        small_prompt = self.folder/'small.md'
        small_prompt.write_text('小額策略',encoding='utf-8')
        small = self.store.add_strategy(small_prompt,initial_twd=5000)
        self.assertEqual(self.store.summary(second)['capital_limit_twd'],50000)
        self.assertEqual(self.store.account_input(self.store.strategies()[2],self.rules)['available_cash_twd'],0)
        self.assertEqual(self.store.summary(small)['cash_twd'],5000)
        strategy = self.store.strategies()[0]
        self.store.record('before-topup',strategy,[self.candidate()],self.decision(),
                          dict(validation='VALID',api={}),self.rules,True)
        self.store.mark_equity('before-topup',[self.first],[self.candidate()])
        before = self.store.summary(self.first)
        after = self.store.increase_capital(self.first,150000)
        self.assertEqual(after['capital_limit_twd'],150000)
        self.assertEqual(after['cash_twd'],before['cash_twd']+50000)
        self.assertEqual(after['equity_twd'],before['equity_twd']+50000)
        self.assertAlmostEqual(after['return_pct'],before['return_pct'])
        self.assertEqual(self.store.summary(second)['cash_twd'],50000)
        self.assertEqual([e['event_type'] for e in self.store.capital_history(self.first)],
                         ['INITIAL','INCREASE'])
        with self.assertRaises(ValueError):
            self.store.increase_capital(self.first,100000)

    def test_delete_strategy_clears_only_its_account_history_and_run_file(self):
        other_prompt = self.folder/'other.md'
        other_prompt.write_text('另一策略',encoding='utf-8')
        second = self.store.add_strategy(other_prompt,initial_twd=50000)
        for strategy in self.store.strategies():
            self.store.record('shared-run',strategy,[self.candidate()],self.decision(),
                              dict(validation='VALID',api={}),self.rules,True)
        self.store.mark_equity('shared-run',[self.first,second],[self.candidate()])
        run_dir = self.store.runs_path/'shared-run'
        run_dir.mkdir(parents=True)
        (run_dir/f'{self.first}.json').write_text('{}',encoding='utf-8')
        (run_dir/f'{second}.json').write_text('{}',encoding='utf-8')
        (run_dir/'run.json').write_text(json.dumps({'strategy_results':{
            self.first:{},second:{}},'assumptions':{'strategy_capital':{
                self.first:100000,second:50000}}}),encoding='utf-8')
        self.store.delete_strategy(self.first)
        self.assertEqual([s['id'] for s in self.store.strategies()],[second])
        with closing(sqlite3.connect(self.store.path)) as db:
            for table in ('positions','cycles','orders','trades','equity','capital_events'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table} WHERE strategy_id=?',
                                            (self.first,)).fetchone()[0],0)
                self.assertGreater(db.execute(f'SELECT COUNT(*) FROM {table} WHERE strategy_id=?',
                                              (second,)).fetchone()[0],0)
        self.assertFalse((run_dir/f'{self.first}.json').exists())
        self.assertTrue((run_dir/f'{second}.json').exists())
        remaining = json.loads((run_dir/'run.json').read_text(encoding='utf-8'))
        self.assertEqual(list(remaining['strategy_results']),[second])
        self.assertEqual(list(remaining['assumptions']['strategy_capital']),[second])
        self.store.delete_strategy(second)
        self.assertFalse(run_dir.exists())

    def test_usd_rates_can_be_left_blank(self):
        other = ExperimentStore(self.folder/'blank.sqlite3')
        other.configure(100000, '', '')
        self.assertIsNone(other.settings()['input_usd_per_million'])
        self.assertIsNone(other.settings()['output_usd_per_million'])
        other.configure(100000, '', 8)
        self.assertIsNone(other.settings()['input_usd_per_million'])
        self.assertEqual(other.settings()['output_usd_per_million'],'8')
        sid = other.add_strategy(self.prompt)
        strategy = other.strategies()[0]
        response = dict(validation='VALID',api={'usage':{'input_tokens':1000,'output_tokens':200}})
        other.record('blank-rate',strategy,[self.candidate()],self.decision(),response,self.rules,False)
        self.assertIsNone(other.summary(sid)['api_cost_usd'])
        self.assertEqual(other.summary(sid)['input_tokens'],1000)
        other.configure(100000, 2, 8)
        self.assertEqual(other.summary(sid)['api_cost_usd'],Decimal('0.0036'))

    def test_independent_wallet_cost_and_no_duplicate_fill(self):
        second_prompt = self.folder/'other.md'
        second_prompt.write_text('另一策略',encoding='utf-8')
        second = self.store.add_strategy(second_prompt)
        strategy = self.store.strategies()[0]
        response = dict(validation='VALID',api={'usage':{'input_tokens':1000,'output_tokens':200}})
        self.store.record('cycle-1',strategy,[self.candidate()],self.decision(),response,self.rules,True)
        self.store.mark_equity('cycle-1',[self.first,second],[self.candidate()])
        self.assertEqual(self.store.positions(self.first)[0]['qty'],10)
        self.assertEqual(self.store.positions(second),[])
        self.assertEqual(self.store.summary(second)['cash_twd'],100000)
        self.assertEqual(self.store.summary(self.first)['trade_count'],1)
        self.assertEqual(self.store.summary(self.first)['api_cost_usd'],Decimal('0.0036'))
        self.assertEqual(len(self.store.recent_trades(self.first)),1)
        with self.assertRaises(ValueError):
            self.store.record('cycle-1',strategy,[self.candidate()],self.decision(),response,self.rules,True)
        self.assertEqual(len(self.store.recent_trades(self.first)),1)

    def test_stale_quote_cannot_fill(self):
        stale = '2000-01-01T12:00:00+08:00'
        strategy = self.store.strategies()[0]
        self.store.record('stale',strategy,[self.candidate(stale)],self.decision(),
                          dict(validation='VALID',api={}),self.rules,True)
        self.assertEqual(self.store.positions(self.first),[])
        self.assertEqual(self.store.summary(self.first)['trade_count'],0)

    def test_late_join_has_no_backfilled_equity(self):
        self.store.mark_equity('before',[self.first],[self.candidate()])
        self.prompt.write_text('第二版策略',encoding='utf-8')
        later = self.store.add_strategy(self.prompt)
        self.store.mark_equity('after',[self.first,later],[self.candidate()])
        rows = self.store.equity_series()
        self.assertEqual([r['run_id'] for r in rows if r['strategy_id']==later],['after'])
        self.assertEqual(len([r for r in rows if r['strategy_id']==self.first]),2)

    def test_daily_cap_and_minimum_cash_are_enforced(self):
        strategy = self.store.strategies()[0]
        self.store.record('large',strategy,[self.candidate()],self.decision(qty=999),
                          dict(validation='VALID',api={}),self.rules,True)
        trade = self.store.recent_trades(self.first)[0]
        self.assertLessEqual(trade['reserved_cents'],2000000)
        self.assertGreaterEqual(self.store.summary(self.first)['cash_twd'],10000)

    def test_fill_uses_observed_ask_and_bid_without_extra_slippage(self):
        strategy = self.store.strategies()[0]
        quote = self.candidate()
        quote.update(ask=101,bid=99)
        response = dict(validation='VALID',api={})
        self.store.record('buy',strategy,[quote],self.decision(),response,self.rules,True)
        self.assertEqual(self.store.recent_trades(self.first)[0]['price_cents'],10100)
        self.store.record('sell',strategy,[quote],self.decision('SELL'),response,self.rules,True)
        self.assertEqual(self.store.recent_trades(self.first)[0]['price_cents'],9900)

    def test_fixed_rule_is_validated_by_same_contract(self):
        c = self.candidate()
        account = self.store.account_input(self.store.strategies()[0],self.rules)
        c['max_buy_qty'] = 100
        result = fixed_rule_decision([c],account)
        position_analyzer.validate(result,[c],account['available_cash_cents'],
                                   self.rules.min_buy_fee_reserve_twd,
                                   self.rules.buy_cash_buffer_rate)
        self.assertEqual(result['decisions'][0]['decision'],'BUY')

    def test_one_shared_market_cycle_runs_two_isolated_strategies(self):
        rule_id = self.store.add_strategy(ROOT/'strategies'/'fixed_rules.md','RULE')
        requests = []
        quote_calls = []
        def provider(symbols):
            quote_calls.append(list(symbols))
            return {}, {}
        def ai_provider(request):
            requests.append(request)
            return json.dumps(self.decision(),ensure_ascii=False), {
                'usage':{'input_tokens':100,'output_tokens':50}}
        quote = self.candidate()
        quote['volume'] = 1000
        batch = dict(quotes=[quote],symbols_received=1)
        top = [dict(symbol='0050',rank=1,quant_score=10)]
        def union(*args,**kwargs):
            positions = args[8]
            c = dict(quote)
            c['paper_position'] = next((p for p in positions if p['symbol']=='0050'),
                                       dict(qty=0,cost_cents=0))
            c['position'] = dict(ai_managed_qty=c['paper_position']['qty'])
            return [c]
        service = ExperimentService(self.store,quote_provider=provider,ai_provider=ai_provider)
        with patch('strategy_experiment.RUNS',self.folder/'runs'), \
             patch('strategy_experiment.load_universe',return_value={'symbols':['0050'],'etf_symbols':[]}), \
             patch('strategy_experiment.load_rules',return_value={}), \
             patch('strategy_experiment.multi_snapshot',return_value=batch), \
             patch('strategy_experiment.scan_and_rank',return_value=([],top,top)), \
             patch('strategy_experiment.build_union',side_effect=union), \
             patch('strategy_experiment.should_call_openai',return_value=True):
            result = service.run()
        self.assertEqual(len(quote_calls),1)
        self.assertEqual(len(requests),1)
        self.assertEqual(self.store.positions(self.first)[0]['qty'],10)
        self.assertGreater(self.store.positions(rule_id)[0]['qty'],10)
        self.assertFalse(result['manifest']['broker_order_sent'])
        self.assertEqual(len(result['summaries']),2)


if __name__ == '__main__':
    unittest.main()
