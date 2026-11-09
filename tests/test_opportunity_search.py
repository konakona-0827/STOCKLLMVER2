from copy import deepcopy
from dataclasses import replace
from datetime import date,datetime,timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from config import TAIPEI
from market_calendar import previous_trading_day
from opportunity_engine import refresh,select,policy,match_setup,allocation_limit
from opportunity_gate import evaluate,record
from pretrade_policy import price_levels
from strategy_evidence import assess
from position_analyzer import validate
from execution.auto_advice_executor import LiveExecutionConfig,check_setup_allocation
from execution.risk_guard import RiskRejected


def candidate(symbol='TEST'):
    day=date(2026,11,3);dates=[]
    for _ in range(22):
        day=previous_trading_day(day);dates.append(day.isoformat())
    dates.reverse();bars=[]
    for i,day in enumerate(dates):
        close=100+i
        bars.append(dict(date=day,open=close-1,high=close+2,low=close-2,close=close,
                         volume=1000,complete=True,source='TWSE_DAILY'))
    bars[-1].update(open=121,high=124,low=120,close=124,volume=2500)
    c=dict(symbol=symbol,name='測試',asset_type='STOCK',last_price=125,bid=124.9,ask=125,
           open=123,high=126,low=120,volume=10000,quote_status='LIVE',is_trial=False,
           quote_timestamp='2026-11-03T10:00:00+08:00',pricing_policy='PLANNED_LIMIT',
           daily_bars=bars,market_history=[],paper_position=dict(qty=0),position=dict(ai_managed_qty=0),
           max_buy_qty=99,max_sell_qty=0,quant_rank=1)
    e=assess(c,[]);c['strategy_evidence']=e;c['eligible_strategies']=e['eligible_strategies']
    c['price_levels']=price_levels(c)
    return c


def decision(c):
    s=c['trade_setups'][0]
    return dict(symbol=c['symbol'],decision='BUY',confidence=.7,action_ratio=0,suggested_qty=s['max_qty'],
        reference_price=c['last_price'],suggested_price=s['entry'],strategy_id=s['strategy_id'],
        target_price=s['target'],invalidation_price=s['invalidation'],holding_days=s['holding_days'],
        reason='確認完成日線突破與量能；目標為估算情境',data_quality='LIVE',warnings=['預算及目標風險已估算'])


class OpportunitySearchTests(unittest.TestCase):
    def setUp(self):
        self.p=policy();self.account=dict(available_cash_cents=500000,available_cash_twd=5000,
             capital_limit_twd=10000,budget_status='READY')
        self.rules=LiveExecutionConfig(minimum_commission_twd=Decimal(1))

    def test_breakout_has_account_sized_plan_without_inventing_an_observed_target(self):
        c=refresh(candidate(),self.account,self.rules,self.p)
        self.assertTrue(c['trade_setups'],c['opportunity_status'])
        for s in c['trade_setups']:
            self.assertTrue(s['daily_breakout_confirmed'])
            self.assertEqual(s['target_kind'],'MODELED_R_MULTIPLE_NOT_OBSERVED_PRICE')
            self.assertLessEqual(s['funding_required_twd'],5000)
            self.assertLessEqual(s['estimated_net_downside_twd'],50)
            self.assertGreaterEqual(s['net_return'],.02)
            self.assertGreaterEqual(s['reward_risk_ratio'],1.5)
            self.assertGreaterEqual(s['entry']-s['invalidation'],s['atr14']*.6)
            self.assertLessEqual(s['target']-s['entry'],s['atr14']*4+.01)
        d=decision(c);self.assertIsNotNone(match_setup(c,d))
        validate(dict(market_view='測試',decisions=[d]),[c],500000,1,.01,capital_twd=10000,
                 funding_fee_reserve_twd=20)
        self.assertEqual(d['decision'],'BUY')

    def test_unconfirmed_volume_uses_early_tier_and_intervening_resistance_remains_required(self):
        c=candidate();c['daily_bars'][-1]['volume']=1000
        early=refresh(c,self.account,self.rules,self.p)['trade_setups']
        self.assertTrue(early)
        self.assertTrue(all(s['tier']=='Early' and not s['daily_breakout_confirmed'] for s in early))
        c=candidate();c['daily_bars'][3]['high']=135;c['price_levels']=price_levels(c)
        result=refresh(c,self.account,self.rules,self.p)
        self.assertTrue(result['trade_setups'],result['opportunity_status'])
        self.assertEqual(result['trade_setups'][0]['target'],135)
        self.assertEqual(result['trade_setups'][0]['target_kind'],'OBSERVED_SWING_RESISTANCE')

    def test_broken_historical_stop_rejected_but_fresh_today_low_remains_available(self):
        c=candidate();c.update(last_price=135,ask=135,bid=134.9,open=132,high=136,low=120)
        c['price_levels']=[dict(id='low_20d',price=128,basis='past'),
            dict(id='high_20d',price=155,basis='past'),dict(id='today_low',price=120,basis='LIVE')]
        result=refresh(c,self.account,self.rules,self.p)
        self.assertTrue(result['opportunity_status']['rejected_setups'])
        self.assertFalse(any(s['invalidation']==128 for s in result['trade_setups']))
        c['low']=128;c['price_levels']=[dict(id='today_low',price=128,basis='LIVE'),
            dict(id='high_20d',price=155,basis='past')]
        result=refresh(c,self.account,self.rules,self.p)
        self.assertTrue(any(s['invalidation']==128 for s in result['trade_setups']),result['opportunity_status'])

    def test_pretrade_cost_evidence_preserves_integer_rounding_at_marginal_hurdle(self):
        from trade_economics import evaluate_buy
        from position_analyzer import review_trades
        c=dict(symbol='X',asset_type='STOCK',ask=27.6,bid=27.55,pricing_policy='PLANNED_LIMIT')
        e=evaluate_buy(c,qty=41,limit_price=27.6,target_price=29.6,invalidation_price=26.6)
        self.assertEqual(e['estimated_buy_commission_twd'],1)
        self.assertEqual(e['estimated_target_tax_twd'],4)
        self.assertAlmostEqual(e['reward_risk_ratio'],73.95/49.05)
        captured=[]
        def approve(request):
            captured.append(json.loads(request['input']))
            return json.dumps(dict(reviews=[dict(symbol='X',approved=True,reason='same rounding verified')])),{}
        d=dict(symbol='X',decision='BUY',suggested_qty=41,suggested_price=27.6,target_price=29.6,
            invalidation_price=26.6)
        review_trades(dict(decisions=[d]),dict(model='test',input=json.dumps(dict(analysis_set=[c]))),approve)
        self.assertEqual(captured[0]['computed_checks'][0]['commission_rounding'],'floor_to_whole_TWD_before_minimum')
        self.assertEqual(d['decision'],'BUY')

    def test_future_bars_gaps_stale_quotes_and_losing_additions_cannot_create_a_buy(self):
        c=candidate();base=deepcopy(refresh(deepcopy(c),self.account,self.rules,self.p)['trade_setups'])
        c['daily_bars'].append(dict(date='2026-11-04',high=999,low=1,close=999,volume=999999,
                                    complete=True,source='TWSE_DAILY'))
        self.assertEqual(refresh(c,self.account,self.rules,self.p)['trade_setups'],base)
        c=candidate();c['daily_bars'].pop(-2)
        self.assertFalse(refresh(c,self.account,self.rules,self.p)['trade_setups'])
        for patch in (dict(quote_status='LAST_KNOWN'),dict(is_trial=True),dict(
                paper_position=dict(qty=1),ai_cost_basis=dict(available=True,average_unit_cost_twd=130)),
                dict(pending_execution=True)):
            self.assertFalse(refresh({**candidate(),**patch},self.account,self.rules,self.p)['trade_setups'])

    def test_allocation_uses_fixed_capital_and_deducts_existing_position(self):
        c=candidate()
        self.assertEqual(allocation_limit(c,self.account,self.p),5000)
        other={**self.account,'available_cash_cents':400000}
        self.assertEqual(allocation_limit(c,other,self.p),4000)
        c['paper_position']['qty']=10
        self.assertEqual(allocation_limit(c,self.account,self.p),3750)
        self.assertEqual(allocation_limit(c,{**self.account,'available_cash_cents':100000},self.p),1000)

    def test_capital_and_risk_sized_research_is_bounded_and_keeps_holdings(self):
        pool=[candidate('X'+str(i)) for i in range(5)]
        holding=candidate('HELD');holding['paper_position']['qty']=30
        pool.append(holding)
        selected,audit=select(pool,self.account,self.rules,self.p)
        self.assertEqual(len(selected),4)
        self.assertIn('HELD',[c['symbol'] for c in selected])
        self.assertEqual(audit['pool_count'],6)
        self.assertEqual(len(audit['qualified_symbols']),5)

    def test_model_cannot_raise_the_target_or_size_to_manufacture_a_buy(self):
        for field in ('target_price','suggested_qty'):
            c=refresh(candidate(),self.account,self.rules,self.p);d=decision(c);d[field]+=1
            validate(dict(market_view='測試',decisions=[d]),[c],500000,1,.01,capital_twd=10000,
                     funding_fee_reserve_twd=20)
            self.assertEqual(d['decision'],'WAIT')
            self.assertIn('BUY_MUST_MATCH_CURRENT_ACCOUNT_SIZED_SETUP',d['warnings'])

    def test_reduced_size_must_still_cover_the_research_economics_hurdle(self):
        c=refresh(candidate(),self.account,self.rules,self.p);d=decision(c);d['suggested_qty']=5
        validate(dict(market_view='測試',decisions=[d]),[c],500000,1,.01,capital_twd=10000,
                 funding_fee_reserve_twd=20)
        self.assertEqual(d['decision'],'WAIT')
        self.assertIn('BUY_TARGET_PROFIT_BELOW_RESEARCH_BUDGET_HURDLE',d['warnings'])

    def test_commission_and_funding_reserve_are_independent_and_legacy_config_is_preserved(self):
        self.assertEqual(self.rules.min_buy_fee_reserve_twd,20)
        self.assertEqual(self.rules.economic_minimum_commission_twd,1)
        legacy=replace(self.rules,minimum_commission_twd=None)
        self.assertEqual(legacy.economic_minimum_commission_twd,20)
        self.assertFalse(refresh(candidate(),self.account,legacy,self.p)['trade_setups'])

    def test_refresh_is_stable_and_positions_outside_pool_cannot_become_buy_candidates(self):
        c=refresh(candidate(),self.account,self.rules,self.p)
        before=deepcopy(c['price_levels']);plans=deepcopy(c['trade_setups'])
        for _ in range(3):refresh(c,self.account,self.rules)
        self.assertEqual(c['price_levels'],before)
        self.assertEqual(c['trade_setups'],plans)
        c['opportunity_buy_eligible']=False
        self.assertFalse(refresh(c,self.account,self.rules)['trade_setups'])

    def test_refreshed_executor_rechecks_tier_cap_and_existing_exposure(self):
        q=SimpleNamespace(ask=Decimal(125),bid=Decimal('124.9'));plan=SimpleNamespace(estimated_value_twd=Decimal(1000))
        recommendation=dict(entry_setup=dict(tier='Early',allocation_limit_twd=1500),
                            asset_type='STOCK',buy_plan=dict(invalidation_price=120))
        check_setup_allocation(q,plan,recommendation,10000,0,self.rules)
        with self.assertRaises(RiskRejected):
            check_setup_allocation(q,plan,recommendation,10000,21,self.rules)

    def test_repeated_scans_cannot_reset_an_existing_symbols_stop_risk(self):
        c=candidate();c['paper_position']['qty']=8
        c['ai_cost_basis']=dict(available=True,average_unit_cost_twd=124)
        c['buy_plans']=dict(lots=[dict(plan=dict(invalidation_price=120))])
        self.assertFalse(refresh(c,self.account,self.rules,self.p)['trade_setups'])

    def test_no_entry_and_no_changed_holding_skips_paid_analysis_but_risk_trigger_wakes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json';at=datetime(2026,11,3,10,tzinfo=TAIPEI)
            self.assertFalse(evaluate('AUTO',[],self.account,path,self.p,at)['call_model'])
            c=candidate();c['paper_position']['qty']=10
            c['buy_plans']=dict(lots=[dict(plan=dict(invalidation_price=120,target_price=140))])
            self.assertTrue(evaluate('AUTO',[c],self.account,path,self.p,at)['call_model'])
            record(path,[c],self.account,at)
            self.assertFalse(evaluate('AUTO',[c],self.account,path,self.p,at+timedelta(hours=1))['call_model'])
            self.assertTrue(evaluate('MANUAL',[c],self.account,path,self.p,at+timedelta(hours=1))['call_model'])
            c['bid']=120
            self.assertEqual(evaluate('AUTO',[c],self.account,path,self.p,at+timedelta(minutes=1))['reason'],
                             'ORIGINAL_EXIT_LEVEL_TOUCHED')
            path.write_text('broken',encoding='utf-8')
            self.assertTrue(evaluate('AUTO',[c],self.account,path,self.p,at)['call_model'])

    def test_reachable_new_setup_is_an_immediate_paid_research_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json';at=datetime(2026,11,3,10,tzinfo=TAIPEI)
            record(path,[],self.account,at)
            c=refresh(candidate(),self.account,self.rules,self.p)
            self.assertTrue(evaluate('AUTO',[c],self.account,path,self.p,at+timedelta(minutes=1))['call_model'])
            record(path,[c],self.account,at)
            self.assertFalse(evaluate('AUTO',[c],self.account,path,self.p,at+timedelta(hours=1))['call_model'])
            self.assertTrue(evaluate('AUTO',[c],self.account,path,self.p,at+timedelta(hours=2))['call_model'])
            noise=deepcopy(c)
            noise['trade_setups'][0]['entry']+=.01
            noise['trade_setups'][0]['setup_id']='different_hash_small_tick'
            self.assertFalse(evaluate('AUTO',[noise],self.account,path,self.p,at+timedelta(hours=1))['call_model'])

    def test_changed_sellable_quantity_wakes_a_position_review_without_a_price_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json';at=datetime(2026,11,3,10,tzinfo=TAIPEI)
            c=candidate();c['paper_position']['qty']=10;c['max_sell_qty']=0
            record(path,[c],self.account,at)
            self.assertFalse(evaluate('AUTO',[c],self.account,path,self.p,at+timedelta(minutes=1))['call_model'])
            c['max_sell_qty']=10
            self.assertEqual(evaluate('AUTO',[c],self.account,path,self.p,at+timedelta(minutes=2))['reason'],
                             'HOLDING_CHANGED_OR_UNKNOWN_PRICE')

    def test_qualified_buy_survives_real_agent_validation_and_two_research_checks(self):
        self.exercise_qualified_buy(candidate())

    def test_first_day_breakout_buy_survives_both_specialists_and_independent_pretrade_review(self):
        c=candidate()
        c['daily_bars'][-1].update(open=121,high=124,low=120,close=121,volume=1000)
        c['price_levels']=price_levels(c)
        self.exercise_qualified_buy(c)

    def exercise_qualified_buy(self,source):
        from trading_agent import TradingTools,analyze
        from support_scout.trading import SupportCoordinator
        with tempfile.TemporaryDirectory() as tmp:
            c=refresh(source,self.account,self.rules,self.p)
            context=TradingTools([c],self.rules,'test',Path(tmp)/'tools.json',lambda symbol,old:deepcopy(old),
                lambda:{**deepcopy(self.account),'positions':[]},lambda:[],entry_skill_enabled=True)
            settings=dict(enabled=True,policy='advisory',history_bars=100,max_symbols=2,max_calls=4,
                          timeout_seconds=3,max_output_tokens=1800)
            context.support=SupportCoordinator(context,Path(tmp),settings,lambda *args:dict(
                status='RAW_PRICE_OBSERVATION',technical=None,limitations=['CORPORATE_ACTIONS_UNVERIFIED'],
                history_status=dict(latest_date='2026-11-02')))
            async def runner(ctx,*args):
                self.assertEqual(ctx.entry_research_skill['name'],'tw-entry-research')
                self.assertIn('tw-entry-research',args[0])
                ctx.get_portfolio();ctx.get_trade_history()
                for role in ('researcher','reviewer'):
                    packet=ctx.support.prepare(role,c['symbol'])
                    ctx.support.accept(role,c['symbol'],dict(verdict='WATCH',
                        evidence_ids=packet['core']['required_confirmation_evidence'],summary='測試獨立檢查',
                        risks=['目標為情境估算'],missing_data=['公司行動未核實']))
                fresh=ctx.get_market_data(c['symbol'])
                return dict(market_view='測試可執行進場',decisions=[decision(fresh)]),{}
            def reviewer(request):
                visible=json.loads(request['input'])['original_input']['analysis_set'][0]
                self.assertEqual(visible['trade_setups'][0]['target_kind'],'MODELED_R_MULTIPLE_NOT_OBSERVED_PRICE')
                return json.dumps(dict(reviews=[dict(symbol=c['symbol'],approved=True,reason='測試條件一致')])),{}
            result,response=analyze(context,'機會策略',runner=runner,reviewer=reviewer)
            self.assertEqual(response['validation'],'VALID',response)
            self.assertEqual(result['decisions'][0]['decision'],'BUY')
            self.assertEqual(response['pretrade_review']['status'],'VALID')
            self.assertEqual(len(response['support_research']['calls']),2)
            research=response['entry_research']
            self.assertEqual(research['skill']['name'],'tw-entry-research')
            self.assertEqual(research['entries'][0]['decision'],'BUY')
            self.assertEqual(len(research['entries'][0]['independent_research']),2)
            self.assertTrue(all(row['evidence_ids'] for row in research['entries'][0]['independent_research']))
            self.assertTrue(research['entries'][0]['pretrade_reviews'][0]['approved'])
            self.assertTrue((Path(tmp)/'entry_research.json').is_file())


if __name__=='__main__':unittest.main()
