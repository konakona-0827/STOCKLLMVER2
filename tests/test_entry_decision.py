from copy import deepcopy
from unittest.mock import patch
import unittest

from entry_decision import review,policy


class EntryDecisionTests(unittest.TestCase):
    def setUp(self):
        self.p=dict(policy(),enabled=True)
        self.c=dict(symbol='X',last_price=105,quote_timestamp='2026-11-03T10:00:00+08:00',market_history=[])
        self.bars=[dict(date='2026-10-'+str(20+i),open=99,close=100) for i in range(5)]
        self.bars[-1].update(open=100,close=102)
        self.s=dict(setup_family='MULTI_SESSION_RECOVERY',target_kind='OBSERVED_SWING_RESISTANCE',
            entry=105,target=115,atr14=3,holding_days=2,breakout_reference_price=104)

    def test_recovery_needs_completed_rebound_and_live_ma5_reclaim(self):
        self.assertTrue(review(self.c,self.s,self.bars,self.p)['approved'])
        self.bars[-1].update(open=103)
        self.assertIn('COMPLETED_DAILY_REBOUND_NOT_OBSERVED',review(self.c,self.s,self.bars,self.p)['blockers'])
        self.c['last_price']=99
        self.assertIn('RECOVERY_MA5_NOT_RECLAIMED',review(self.c,self.s,self.bars,self.p)['blockers'])

    def quote(self,**changes):
        return dict(dict(symbol='X',quote_status='LIVE',is_trial=False,retrieval_mode='BROKER',
            exchange_time='2026-11-03T09:30:00+08:00',price=104.5),**changes)

    def breakout(self):
        self.s.update(setup_family='FIRST_DAY_BREAKOUT',target_kind='MODELED_R_MULTIPLE_NOT_OBSERVED_PRICE')

    def test_first_breakout_is_research_until_distinct_verified_continuation_exists(self):
        self.breakout();self.assertFalse(review(self.c,self.s,self.bars,self.p)['approved'])
        self.c['market_history']=[self.quote()]
        r=review(self.c,self.s,self.bars,self.p)
        self.assertTrue(r['approved']);self.assertIsNone(r['facts']['target_probability'])

    def test_unknown_trial_cached_wrong_symbol_future_and_duplicate_quotes_do_not_confirm(self):
        self.breakout()
        for changes in (dict(quote_status='UNKNOWN'),dict(is_trial=True),dict(retrieval_mode='CACHE'),
                dict(symbol='Y'),dict(exchange_time=self.c['quote_timestamp']),
                dict(exchange_time='2026-11-03T10:01:00+08:00'),dict(exchange_time='2026-11-02T09:30:00+08:00')):
            self.c['market_history']=[self.quote(**changes)]
            self.assertFalse(review(self.c,self.s,self.bars,self.p)['approved'],changes)

    def test_failed_breakout_between_observations_invalidates_prior_continuation(self):
        self.breakout();self.c['market_history']=[self.quote(),self.quote(exchange_time='2026-11-03T09:40:00+08:00',price=103)]
        self.assertFalse(review(self.c,self.s,self.bars,self.p)['approved'])

    def test_strong_trend_pullback_does_not_inherit_breakout_or_recovery_requirements(self):
        self.s['setup_family']='TREND_PULLBACK';self.bars[-1]['open']=110
        self.assertTrue(review(self.c,self.s,self.bars,self.p)['approved'])

    def test_enabled_guard_is_applied_on_quote_refresh_and_returns_when_confirmation_arrives(self):
        from test_opportunity_search import candidate
        from opportunity_engine import refresh,policy as opportunity_policy
        from execution.auto_advice_executor import LiveExecutionConfig
        c=candidate();c['daily_bars'][-1].update(open=121,high=124,low=120,close=121,volume=1000)
        from pretrade_policy import price_levels
        c['price_levels']=price_levels(c)
        account=dict(available_cash_cents=1000000,capital_limit_twd=10000,budget_status='READY')
        with patch('entry_decision.policy',return_value=self.p):
            refresh(c,account,LiveExecutionConfig(minimum_commission_twd=1),opportunity_policy())
            self.assertFalse(c['trade_setups'])
            self.assertIn('ENTRY_DECISION_EVIDENCE_NOT_CONFIRMED',c['opportunity_status']['blockers'])
            c['market_history']=[dict(self.quote(symbol=c['symbol']),price=124.5)]
            refresh(c,account,LiveExecutionConfig(minimum_commission_twd=1),opportunity_policy())
            self.assertTrue(c['trade_setups'],c['opportunity_status'])
