"""Past-only target evidence, pending research, and quote provenance boundaries."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from entry_decision import policy as entry_policy,review
from opportunity_engine import refresh,policy,select
from target_reachability import profile
from test_opportunity_search import candidate
from execution.auto_advice_executor import LiveExecutionConfig
from pretrade_policy import price_levels


class TargetReachabilityTests(unittest.TestCase):
    def setUp(self):
        self.c=candidate()
        self.account=dict(available_cash_cents=1000000,available_cash_twd=10000,capital_limit_twd=10000,budget_status='READY')
        self.rules=LiveExecutionConfig(minimum_commission_twd=1)

    def reference(self,c=None,days=10,dates=None):
        return profile((c or self.c)['daily_bars'],as_of='2026-11-03',holding_days=days,
                       trend=True,session_dates=dates)

    def test_reference_is_anchored_to_past_close_and_changes_with_horizon_not_entry_or_stop(self):
        original=self.reference()
        self.assertEqual(original['status'],'AVAILABLE')
        changed=deepcopy(self.c);changed.update(ask=140,last_price=140,low=130)
        self.assertEqual(self.reference(changed),original)
        self.assertLess(self.reference(days=2)['price_ceiling'],original['price_ceiling'])
        self.assertIsNone(original['target_probability'])
        self.assertLessEqual(original['available_windows'],22)
        ordered=sorted(original['windows'],key=lambda w:w['anchor_date'])
        self.assertTrue(all(a['end_date']<=b['anchor_date'] for a,b in zip(ordered,ordered[1:])))

    def test_future_rows_cannot_change_reference_or_historical_windows(self):
        before=self.reference()
        self.c['daily_bars'].append(dict(date='2026-11-03',open=1,low=1,high=9999,close=9999,
                                        complete=True,source='TWSE_DAILY'))
        self.assertEqual(self.reference(),before)
        self.assertTrue(all(w['end_date']<'2026-11-03' for w in before['windows']))

    def test_short_history_and_unverified_calendar_do_not_fabricate_a_range(self):
        self.c['daily_bars']=self.c['daily_bars'][-22:]
        self.assertEqual(self.reference()['status'],'INSUFFICIENT_PAST_WINDOWS')
        self.assertNotIn('price_ceiling',self.reference())
        self.assertEqual(self.reference(dates=[])['status'],'MARKET_CALENDAR_UNVERIFIED')

    def test_event_matching_precedes_spacing_and_never_calls_generic_trends_breakout_analogues(self):
        for i,bar in enumerate(self.c['daily_bars']):bar['volume']=10000 if i%12==7 else 1000
        result=profile(self.c['daily_bars'],as_of='2026-11-03',holding_days=10,trend=True,completed_breakout=True)
        self.assertEqual(result['conditioning'],'SAME_COMPLETED_BREAKOUT_AND_VOLUME')
        self.assertGreaterEqual(result['matching_completed_breakout_windows'],8)
        self.assertTrue(all(w['trend'] and w['completed_breakout'] for w in result['windows']))
        for bar in self.c['daily_bars']:bar['volume']=1000
        sparse=profile(self.c['daily_bars'],as_of='2026-11-03',holding_days=10,trend=True,completed_breakout=True)
        self.assertNotEqual(sparse['conditioning'],'SAME_COMPLETED_BREAKOUT_AND_VOLUME')
        self.assertEqual(sparse['matching_completed_breakout_windows'],0)

    def test_suspended_sessions_are_gaps_in_forward_windows_not_longer_hidden_horizons(self):
        dates=[b['date'] for b in self.c['daily_bars']]
        before=self.reference(dates=dates)
        missing=self.c['daily_bars'].pop(-40)['date']
        after=self.reference(dates=dates)
        self.assertLess(after['available_windows'],before['available_windows'])
        self.assertFalse(any(w['anchor_date']<missing<=w['end_date'] for w in after['windows']))
        self.c['daily_bars'].pop(-2)
        self.assertEqual(self.reference(dates=dates)['status'],'RECENT_HISTORY_GAP')

    def test_estimates_are_still_available_but_chasing_cannot_move_the_reference_up(self):
        # This failed proposal remains reproducible, but is disabled by default.
        from target_reachability import policy as reachability_policy
        p=dict(reachability_policy(),empirical_target_enabled=True)
        with patch('target_reachability.policy',return_value=p):
            self.exercise_empirical_proposal()

    def exercise_empirical_proposal(self):
        result=refresh(self.c,self.account,self.rules,policy())
        self.assertTrue(result['trade_setups'],result['opportunity_status'])
        plans=[s for s in result['trade_setups'] if 'EMPIRICAL' in s['target_kind']]
        self.assertTrue(plans)
        reference=result['target_reachability']['price_ceiling']
        self.assertTrue(all(s['target']<=reference for s in plans))
        initial=next(s['target'] for s in plans if s['entry_basis']=='CURRENT_ASK')
        self.c.update(last_price=126,ask=126,bid=125.5)
        changed=refresh(self.c,self.account,self.rules,policy())
        self.assertEqual(next(s['target'] for s in changed['trade_setups'] if s['entry_basis']=='CURRENT_ASK'),initial)
        self.c.update(last_price=160,ask=160,bid=159.5,open=150,high=161,low=145)
        self.c['price_levels']=price_levels(self.c)
        result=refresh(self.c,self.account,self.rules,policy())
        self.assertEqual(result['target_reachability']['price_ceiling'],reference)
        self.assertFalse(result['trade_setups'])
        self.assertIn('NO_REMAINING_UPSIDE_WITHIN_HORIZON',result['opportunity_status']['rejection_reasons'])

    def test_default_does_not_turn_a_historical_median_into_a_blanket_veto(self):
        from target_reachability import policy as reachability_policy,assess_target
        self.assertFalse(reachability_policy()['empirical_target_enabled'])
        result=refresh(self.c,self.account,self.rules,policy())
        plan=result['trade_setups'][0]
        self.assertEqual(plan['target_kind'],'REQUIRED_PAYOFF_NOT_OBSERVED_PRICE')
        assessment=plan['target_evidence']['payoff_assessment']
        self.assertFalse(assessment['mandatory_median_veto'])
        self.assertIsNone(assessment['target_probability'])
        historical=result['target_reachability']
        very_far=assess_target(historical,entry=125,target=10000,invalidation=120)
        self.assertEqual(very_far['past_range_support_windows'],0)
        self.assertEqual(very_far['anchor_close'],historical['anchor_close'])
        self.assertGreater(very_far['required_excursion_from_anchor_atr'],assessment['required_excursion_from_anchor_atr'])

    def pending(self):
        self.c['daily_bars'][-1].update(open=121,high=124,low=120,close=121,volume=1000)
        self.c['market_history']=[]
        self.c['price_levels']=price_levels(self.c)
        return self.c

    def test_pending_breakout_remains_researchable_then_real_next_quote_confirms(self):
        from trading_agent import TradingTools
        selected,audit=select([self.pending()],self.account,self.rules,policy())
        self.assertEqual(audit['qualified_symbols'],[])
        self.assertEqual(audit['watch_research_symbols'],[self.c['symbol']])
        self.assertTrue(selected[0]['pending_entry_setups'])
        with tempfile.TemporaryDirectory() as directory:
            tools=TradingTools(selected,self.rules,'test',Path(directory)/'tools.json',None,None,None)
            tools.account=self.account
            updated=tools._accept_quote(self.c['symbol'],dict(symbol=self.c['symbol'],quote_status='LIVE',
                is_trial=False,quote_timestamp='2026-11-03T10:01:00+08:00',last_price=125,ask=125,bid=124.9))
            self.assertEqual(updated['market_history'][-1]['price'],125)
            self.assertTrue(updated['trade_setups'],updated['opportunity_status'])
            count=len(updated['market_history'])
            tools._accept_quote(self.c['symbol'],dict(symbol=self.c['symbol'],quote_status='LIVE',
                is_trial=False,quote_timestamp='2026-11-03T10:01:00+08:00'))
            self.assertEqual(len(self.c['market_history']),count)

    def test_daily_proxy_is_explicit_opt_in_and_never_an_executable_live_quote(self):
        self.c.update(quote_status='HISTORICAL_DAILY_PROXY',execution_permitted=False)
        self.assertFalse(refresh(self.c,self.account,self.rules,policy())['trade_setups'])
        self.assertTrue(refresh(self.c,self.account,self.rules,policy(),research_proxy=True)['trade_setups'])
        from position_analyzer import validate
        from test_opportunity_search import decision
        d=decision(self.c);d['data_quality']='HISTORICAL_DAILY_PROXY'
        with self.assertRaisesRegex(ValueError,'REQUIRES_LIVE_QUOTE'):
            validate(dict(market_view='proxy',decisions=[d]),[self.c],1000000,1,.01,capital_twd=10000,
                     funding_fee_reserve_twd=20)
        self.c['execution_permitted']=True
        self.assertFalse(refresh(self.c,self.account,self.rules,policy(),research_proxy=True)['trade_setups'])

    def test_initial_model_view_keeps_counts_and_full_tool_retains_auditable_history(self):
        from token_budget import candidate_view
        self.c['daily_history_status']=dict(verified_trading_dates=[b['date'] for b in self.c['daily_bars']],
            market_calendar=dict(verified=True,provenance=[dict(url='source',sha256='test') for _ in range(16)]))
        refresh(self.c,self.account,self.rules,policy())
        small=candidate_view(self.c,self.account,self.rules)
        full=candidate_view(self.c,self.account,self.rules,full_history=True)
        self.assertNotIn('windows',small['target_reachability'])
        self.assertEqual(small['daily_history_status']['verified_calendar_sessions'],240)
        self.assertEqual(small['daily_history_status']['market_calendar']['source_months'],16)
        self.assertNotIn('provenance',small['daily_history_status']['market_calendar'])
        self.assertEqual(len(full['daily_bars']['rows']),240)
        self.assertEqual(len(full['daily_history_status']['verified_trading_dates']),240)
        self.assertIn('windows',full['target_reachability'])


if __name__=='__main__':unittest.main()
