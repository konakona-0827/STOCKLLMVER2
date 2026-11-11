"""Profit, ownership, review scheduling and prepared-order boundaries; no network."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from config import TAIPEI
from execution.auto_advice_executor import LiveExecutionConfig, check_refreshed_buy
from execution.models import Side
from execution.risk_guard import MarketQuote, RiskRejected
from execution.planned_exit import contract, matches, verify
from position_analyzer import validate
from position_manager import PositionSupervisor, plan_request, validate_plan
from profit_exit import prepare, refresh, tick_below
from trade_economics import estimate_sell
from pretrade_policy import minimum_net_return
from opportunity_engine import policy as opportunity_policy


AT=datetime(2026,11,11,10,0,tzinfo=TAIPEI)
RULES=LiveExecutionConfig(enabled=True,planned_limits_enabled=True,minimum_commission_twd=Decimal(1))


def holding():
    return dict(symbol='1815',name='測試持倉',asset_type='STOCK',quote_timestamp=AT.isoformat(),
        quote_status='LIVE',is_trial=False,last_price=137.5,bid=137.5,ask=138,open=133,high=140,low=131,
        paper_position=dict(qty=30),position=dict(ai_managed_qty=30),max_sell_qty=30,
        pricing_policy='PLANNED_LIMIT',ai_cost_basis=dict(available=True,average_unit_cost_twd='129'),
        buy_plans=dict(available=True,lots=[dict(quantity=30,original_quantity=30,entry_date='2026-11-10',
            plan=dict(target_price=140.5,invalidation_price=126,holding_days=2))]),market_history=[])


def management(action='HOLD',qty=0):
    return dict(symbol='1815',action=action,sell_quantity=qty,confidence=.7,reason='依實際行情與原計畫評估',
        next_review_seconds=300,watch_above=139,watch_below=135,
        required_data=['RECENT_OBSERVATIONS'])


def health(at=AT,qty=30,bid=137.5):
    q=holding();q.update(quote_timestamp=at.isoformat(),last_price=bid,bid=bid)
    return dict(broker_inventory_verified=True,ai_positions={'1815':qty},
        ai_position_settings={'1815':dict(enabled=True)},position_quotes=[q])


class ProfitExitTests(unittest.TestCase):
    def test_observed_exit_is_ranked_before_a_large_unobserved_projection(self):
        from opportunity_engine import setup_order_key
        observed=dict(target_kind='OBSERVED_RECENT_SESSION_HIGH',priority_score=20)
        projected=dict(target_kind='MODELED_R_MULTIPLE_NOT_OBSERVED_PRICE',priority_score=80)
        self.assertEqual(sorted([projected,observed],key=setup_order_key)[0],observed)

    def test_buy_commission_is_included_before_claiming_a_small_profit(self):
        c=holding();c.update(bid=100.6,paper_position=dict(qty=10),
            ai_cost_basis=dict(available=True,average_unit_cost_twd='100'),buy_plans=dict(lots=[]))
        e=estimate_sell(c,10)
        self.assertEqual(e['estimated_realized_pnl_twd'],0)
        self.assertEqual(e['estimated_allocated_buy_commission_twd'],1)

    def test_multiple_buy_lots_keep_their_individual_minimum_commissions(self):
        c=holding();c.update(bid=130,paper_position=dict(qty=2),
            ai_cost_basis=dict(available=True,average_unit_cost_twd='100'),
            buy_plans=dict(lots=[dict(quantity=1,original_quantity=1),dict(quantity=1,original_quantity=1)]))
        e=estimate_sell(c,2,minimum_commission_twd=20)
        self.assertEqual(e['estimated_allocated_buy_commission_twd'],40)
        self.assertEqual(e['estimated_realized_pnl_twd'],-1)

    def test_paper_basis_does_not_pay_buy_commission_twice(self):
        c=holding();c.update(ai_cost_basis=dict(available=False),paper_position=dict(qty=10,cost_cents=100100))
        e=estimate_sell(c,10)
        self.assertEqual(e['estimated_allocated_buy_commission_twd'],0)
        self.assertEqual(e['cost_basis_source'],'PAPER_INCLUDES_BUY_FEES')

    def test_prepared_exit_is_not_an_order_or_same_day_permission(self):
        c=holding();s=dict(target=140.5,invalidation=126,holding_days=2)
        result=prepare(c,s,RULES)
        self.assertEqual(result['limit_price'],140)
        self.assertFalse(result['broker_order_sent'])
        self.assertEqual(result['activation'],'AFTER_CONFIRMED_FILL_AND_BROKER_SELLABLE')
        c['max_sell_qty']=0
        result=refresh(c,RULES,at=AT)
        self.assertFalse(result['can_sell'])
        self.assertIsNone(result['resting_order'])

    def test_unknown_cost_never_creates_profitable_resting_order(self):
        c=holding();c['ai_cost_basis']=dict(available=False)
        self.assertIsNone(refresh(c,RULES,at=AT)['resting_order'])

    def test_risk_exit_can_be_below_acquisition_cost(self):
        c=holding();c.update(bid=125,last_price=125,ask=125.5)
        r=refresh(c,RULES,at=AT)
        self.assertIn('ORIGINAL_INVALIDATION_TOUCHED',r['triggers'])
        self.assertTrue(r['urgent_review'])
        self.assertLess(r['economics']['estimated_realized_pnl_twd'],0)

    def test_profitable_reversal_does_not_require_original_target(self):
        c=holding()
        c['market_history']=[dict(symbol='1815',exchange_time=(AT-timedelta(minutes=m)).isoformat(),
            price=price,quote_status='LIVE',is_trial=False,retrieval_mode='BROKER_HEALTH')
            for m,price in [(2,139),(1,138.5)]]
        r=refresh(c,RULES,at=AT)
        self.assertIn('POSITIVE_NET_PROFIT_WITH_CONFIRMED_PULLBACK',r['triggers'])
        self.assertTrue(r['urgent_review'])
        self.assertLess(c['bid'],140.5)

    def test_future_cached_trial_and_hourly_samples_do_not_prove_a_reversal(self):
        for change in [dict(is_trial=True),dict(retrieval_mode='CACHE'),dict(minutes=-1),dict(minutes=60)]:
            c=holding();rows=[]
            for minute,price in [(2,139),(1,138.5)]:
                row=dict(symbol='1815',exchange_time=(AT-timedelta(minutes=change.get('minutes',minute))).isoformat(),
                    price=price,quote_status='LIVE',is_trial=False,retrieval_mode='BROKER_HEALTH')
                row.update({k:v for k,v in change.items() if k!='minutes'});rows.append(row)
            c['market_history']=rows
            self.assertNotIn('POSITIVE_NET_PROFIT_WITH_CONFIRMED_PULLBACK',refresh(c,RULES,at=AT)['triggers'])

    def test_pending_order_and_stale_quote_never_authorize_another_sell(self):
        c=holding();c['pending_execution']=True
        self.assertFalse(refresh(c,RULES,at=AT)['can_sell'])
        self.assertIsNone(refresh(c,RULES,at=AT)['resting_order'])
        c['pending_execution']=False
        self.assertFalse(refresh(c,RULES,at=AT+timedelta(minutes=3))['can_sell'])

    def test_valid_bid_without_an_ask_can_still_support_a_profit_sell(self):
        c=holding();c.update(bid=140.5,last_price=140.5,ask=None)
        r=refresh(c,RULES,at=AT)
        self.assertTrue(r['can_sell'])
        self.assertIn('ORIGINAL_TARGET_REACHED',r['triggers'])
        self.assertEqual(r['book_limitation'],'ASK_UNAVAILABLE_SELL_ONLY')

    def test_legal_tick_before_resistance_handles_band_boundaries(self):
        self.assertEqual(tick_below(100,'STOCK'),Decimal('99.9'))
        self.assertEqual(tick_below(50,'STOCK'),Decimal('49.95'))
        self.assertEqual(tick_below(50,'ETF'),Decimal('49.99'))

    def test_resting_exit_revalidates_profit_quantity_time_and_invalidation(self):
        c=holding();refresh(c,RULES,at=AT)
        d=dict(decision='SELL',suggested_qty=30,suggested_price=140)
        self.assertTrue(matches(c,d))
        p=contract(c,d,AT)
        recommendation=dict(exit_plan=p,asset_type='STOCK',ai_managed_qty=30,
            existing_cost_basis=c['ai_cost_basis'],existing_buy_plans=c['buy_plans'])
        intent=SimpleNamespace(side=Side.SELL,suggested_qty=30,suggested_price=Decimal(140))
        q=SimpleNamespace(bid=Decimal('137.5'))
        verify(intent,recommendation,q,AT,1)
        with self.assertRaises(RiskRejected):verify(intent,recommendation,q,AT.replace(hour=14),1)
        with self.assertRaises(RiskRejected):verify(intent,recommendation,SimpleNamespace(bid=Decimal(125)),AT,1)
        recommendation['existing_cost_basis']=dict(available=True,average_unit_cost_twd='141')
        with self.assertRaises(RiskRejected):verify(intent,recommendation,q,AT,1)

    def test_small_profit_uses_the_same_validator_and_send_time_economics(self):
        from test_pretrade_policy import PretradePolicyTests
        helper=PretradePolicyTests();c=helper.candidate();c['opportunity_policy']=opportunity_policy()
        d=helper.decision(qty=10);d.update(target_price=102.5,invalidation_price=99.6,holding_days=2)
        setup=dict(strategy_id=d['strategy_id'],holding_days=2,entry=100,target=102.5,
                   invalidation=99.6,max_qty=10)
        c['trade_setups']=[setup]
        validate(dict(market_view='小幅費後獲利',decisions=[d]),[c],1000000,capital_twd=10000)
        self.assertEqual(d['decision'],'BUY',d)
        e=check_refreshed_buy(SimpleNamespace(ask=Decimal(100),bid=Decimal('99.9')),
            SimpleNamespace(symbol=c['symbol'],quantity=10,limit_price=Decimal(100)),
            dict(asset_type='STOCK',pricing_policy='PLANNED_LIMIT',entry_setup=setup,
                 buy_plan=dict(target_price=102.5,invalidation_price=99.6)),10000,1)
        self.assertLess(e['estimated_net_upside_twd']/e['estimated_buy_cost_twd'],.02)
        self.assertGreaterEqual(e['reward_risk_ratio'],1.5)
        self.assertEqual(minimum_net_return({}),Decimal('.02'))


class PositionManagerTests(unittest.TestCase):
    def test_manager_schema_contains_one_symbol_and_bounded_review_schedule(self):
        c=holding();r=plan_request(c,dict(positions=[dict(symbol='1815',qty=30)]))
        payload=json.loads(r['input'])
        self.assertEqual(payload['candidate']['symbol'],'1815')
        schema=r['text']['format']['schema']['properties']
        self.assertEqual(schema['next_review_seconds']['minimum'],60)
        self.assertEqual(schema['next_review_seconds']['maximum'],1800)
        self.assertEqual(validate_plan(management(),c)['required_data'][0],'LIVE_ODDLOT')

    def test_manager_cannot_extend_schedule_or_sell_unsellable_shares(self):
        c=holding();p=management();p['next_review_seconds']=86400
        with self.assertRaises(ValueError):validate_plan(p,c)
        c['max_sell_qty']=0
        with self.assertRaises(ValueError):validate_plan(management('SELL',1),c)
        p=management();p['watch_below']=140
        with self.assertRaises(ValueError):validate_plan(p,holding())

    def test_new_position_is_reviewed_without_waiting_for_discovery(self):
        entered,release=Event(),Event()
        def reviewer(symbol,previous,observations,stopped):
            entered.set();release.wait(2)
            return dict(management_plan=management(),manifest=dict(run_id='position-test'))
        with tempfile.TemporaryDirectory() as temp:
            manager=PositionSupervisor(temp,reviewer,clock=lambda:AT);manager.set_active(True)
            self.assertEqual(manager.observe(health()),['1815'])
            self.assertTrue(entered.wait(1))
            self.assertTrue(manager.busy)
            self.assertEqual(manager.observe(health()),[])
            release.set()
            for _ in range(100):
                if not manager.busy:break
                Event().wait(.01)
            self.assertFalse(manager.busy)
            self.assertEqual(manager.states['1815']['next_review_at'],(AT+timedelta(seconds=300)).isoformat())
            restored=PositionSupervisor(temp,Mock(),clock=lambda:AT+timedelta(seconds=60));restored.set_active(True)
            self.assertEqual(restored.observe(health(at=AT+timedelta(seconds=60))),[])

    def test_manual_unverified_and_stale_holdings_never_start_a_model(self):
        with tempfile.TemporaryDirectory() as temp:
            reviewer=Mock();manager=PositionSupervisor(temp,reviewer,clock=lambda:AT)
            self.assertEqual(manager.observe(health()),[])
            manager.set_active(True)
            snapshot=health();snapshot['ai_positions']={}
            self.assertEqual(manager.observe(snapshot),[])
            snapshot=health();snapshot['broker_inventory_verified']=False
            self.assertEqual(manager.observe(snapshot),[])
            self.assertEqual(manager.observe(health(at=AT-timedelta(minutes=3))),[])
            reviewer.assert_not_called()

    def test_price_trigger_wakes_before_agent_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            manager=PositionSupervisor(temp,Mock(),clock=lambda:AT+timedelta(seconds=60));manager.set_active(True)
            manager.states['1815']=dict(quantity=30,reviewed_at=AT.isoformat(),
                next_review_at=(AT+timedelta(seconds=1800)).isoformat(),plan=management())
            with patch('position_manager.Thread') as thread:
                self.assertEqual(manager.observe(health(at=AT+timedelta(seconds=60),bid=134)),['1815'])
                self.assertEqual(thread.call_args.kwargs['args'][1],'PRICE_WAKE_LEVEL_TOUCHED')

    def test_position_change_wakes_and_closed_position_removes_old_schedule(self):
        with tempfile.TemporaryDirectory() as temp:
            manager=PositionSupervisor(temp,Mock(),clock=lambda:AT);manager.set_active(True)
            manager.states['1815']=dict(quantity=20,reviewed_at=AT.isoformat(),
                next_review_at=(AT+timedelta(seconds=1800)).isoformat(),plan=management())
            with patch('position_manager.Thread') as thread:
                self.assertEqual(manager.observe(health()),['1815'])
                self.assertEqual(thread.call_args.kwargs['args'][1],'FILLED_POSITION_CHANGED')
            manager.busy=False
            self.assertEqual(manager.observe(health(qty=0)),[])
            self.assertNotIn('1815',manager.states)


if __name__=='__main__':unittest.main()
