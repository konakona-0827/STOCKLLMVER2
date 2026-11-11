"""Coverage and safety at the watch -> research -> executable-plan boundary."""
from copy import deepcopy
from datetime import datetime,timedelta,date
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from config import TAIPEI
from execution.auto_advice_executor import LiveExecutionConfig
from market_discovery import screen,settings,normalize,discover,record_entry_feedback
from opportunity_engine import refresh,select,policy
from opportunity_gate import evaluate,record
from opportunity_watch import observe
from opportunity_diagnostics import summarize
from position_analyzer import validate
from pretrade_policy import price_levels
from strategy_evidence import assess
from test_opportunity_search import candidate,decision
from test_market_discovery import fixtures,answer


def declining(symbol='WATCH'):
    c=candidate(symbol)
    for i,b in enumerate(c['daily_bars']):
        close=121+len(c['daily_bars'])-22-i
        b.update(open=close+.5,high=close+2,low=close-2,close=close,volume=1000)
    c.update(last_price=101,bid=100.9,ask=101,open=102,high=103,low=99)
    c['market_scout']=dict(symbol=symbol,trigger_kind='PULLBACK_TO_CLOSE',condition_price=101,
                           expires_session='2026-11-03')
    return enrich(c)


def enrich(c):
    c['price_levels']=price_levels(c)
    c['eligible_strategies']=assess(c,c.get('market_history',[]))['eligible_strategies']
    return c


class MarketOpportunityRepairTests(unittest.TestCase):
    def setUp(self):
        self.p=policy()
        self.rules=LiveExecutionConfig(minimum_commission_twd=Decimal(1))
        self.account=dict(available_cash_cents=1000000,available_cash_twd=10000,
                          capital_limit_twd=10000,budget_status='READY')
        self.at=datetime(2026,11,3,10,tzinfo=TAIPEI)

    def test_today_first_breakout_has_an_early_modeled_plan_without_fake_daily_volume(self):
        c=candidate()
        c['daily_bars'][-1].update(open=121,high=124,low=120,close=121,volume=1000)
        c=refresh(enrich(c),self.account,self.rules,self.p)
        plans=[s for s in c['trade_setups'] if s['entry_basis']=='CURRENT_ASK']
        self.assertTrue(plans,c['opportunity_status'])
        plan=plans[0]
        self.assertEqual(plan['tier'],'Early')
        self.assertEqual(plan['setup_family'],'FIRST_DAY_BREAKOUT')
        self.assertFalse(plan['daily_breakout_confirmed'])
        self.assertEqual(plan['breakout_reference_price'],124)
        self.assertEqual(plan['target_kind'],'EMPIRICAL_HORIZON_RANGE_NOT_OBSERVED_PRICE')
        self.assertTrue(all(s['entry']>=124 for s in c['trade_setups']))
        self.assertLessEqual(plan['combined_modeled_risk_twd'],50)
        self.assertLessEqual(plan['funding_required_twd'],3500)
        self.assertGreaterEqual(plan['reward_risk_ratio'],1.5)

    def test_high_touch_alone_is_not_a_breakout_and_flat_volume_cannot_claim_daily_confirmation(self):
        c=candidate()
        c['daily_bars'][-1].update(open=121,high=125,low=120,close=121,volume=1000)
        c=refresh(enrich(c),self.account,self.rules,self.p)
        self.assertFalse(any(s['setup_family']=='FIRST_DAY_BREAKOUT' for s in c['trade_setups']))
        self.assertFalse(any(s['target_kind']=='MODELED_R_MULTIPLE_NOT_OBSERVED_PRICE' for s in c['trade_setups']))

    def test_recovery_prefers_real_multiday_resistance_to_intraday_high(self):
        c=declining()
        c.pop('market_scout')
        c.update(last_price=102,bid=101.9,ask=102,open=100,high=103,low=99)
        for b in c['daily_bars'][-5:]:b['high']=110
        c=refresh(enrich(c),self.account,self.rules,self.p)
        plans=[s for s in c['trade_setups'] if s['entry_basis']=='CURRENT_ASK']
        self.assertTrue(plans,c['opportunity_status'])
        self.assertEqual(plans[0]['target'],110)
        self.assertEqual(plans[0]['target_kind'],'OBSERVED_RECENT_SESSION_HIGH')
        self.assertEqual(plans[0]['holding_days'],2)
        self.assertEqual(plans[0]['tier'],'Early')

    def test_pullback_does_not_skip_a_resistance_between_entry_and_current_price(self):
        c=candidate();c['price_levels'].append(dict(id='high_test',price=124,basis='observed'))
        c=refresh(c,self.account,self.rules,self.p)
        attempts=c['trade_setups']+c['opportunity_status']['near_misses']
        pullbacks=[s for s in attempts if s['entry']<124]
        self.assertTrue(pullbacks,c['opportunity_status'])
        self.assertTrue(all(s['target']<=124 for s in pullbacks))

    def test_watch_hit_enters_research_without_executable_plan_and_survives_refresh(self):
        selected,audit=select([declining()],self.account,self.rules,self.p)
        self.assertEqual(audit['qualified_symbols'],[])
        self.assertEqual(audit['watch_research_symbols'],['WATCH'])
        self.assertEqual(len(selected),1)
        c=selected[0]
        self.assertEqual(c['analysis_source'],'WATCH_TRIGGER_RESEARCH')
        self.assertTrue(refresh(c,self.account,self.rules)['research_only'])
        self.assertEqual(c['trade_setups'],[])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json'
            self.assertTrue(evaluate('AUTO',selected,self.account,path,self.p,self.at)['call_model'])
            record(path,selected,self.account,self.at)
            self.assertFalse(evaluate('AUTO',selected,self.account,path,self.p,self.at+timedelta(hours=1))['call_model'])
            self.assertTrue(evaluate('AUTO',selected,self.account,path,self.p,self.at+timedelta(hours=2))['call_model'])
            changed=deepcopy(selected);changed[0]['last_price']=100.5
            self.assertEqual(evaluate('AUTO',changed,self.account,path,self.p,self.at+timedelta(minutes=5))['reason'],
                             'WATCH_RESEARCH_CONDITION_CHANGED')

    def test_watch_cannot_bypass_live_data_budget_cooldown_or_pending_order_guards(self):
        variants=[dict(quote_status='LAST_KNOWN'),dict(is_trial=True),dict(pending_execution=True),
                  dict(opportunity_buy_eligible=False),dict(daily_bars=[]),
                  dict(ai_trade_dates=dict(records=[dict(side='SELL',quantity=1,date='2026-11-02')]))]
        for patch in variants:
            with self.subTest(patch=patch):
                selected,_=select([{**declining(),**patch}],self.account,self.rules,self.p)
                self.assertEqual(selected,[])
        selected,_=select([declining()],{**self.account,'budget_status':'BLOCKED'},self.rules,self.p)
        self.assertEqual(selected,[])

    def test_watch_has_no_permission_to_invent_a_buy(self):
        qualified=refresh(candidate(),self.account,self.rules,self.p)
        d=decision(qualified)
        c=refresh(declining(qualified['symbol']),self.account,self.rules,self.p)
        d['reference_price']=c['last_price']
        validate(dict(market_view='test',decisions=[d]),[c],1000000,1,.01,capital_twd=10000,
                 funding_fee_reserve_twd=20)
        self.assertEqual(d['decision'],'WAIT')
        self.assertEqual(c['trade_setups'],[])

    def test_expired_trial_or_equal_high_watch_never_triggers_research(self):
        c=declining();watch=c['market_scout']
        self.assertEqual(observe({**watch,'expires_session':'2026-11-02'},c)['status'],'EXPIRED_WATCH')
        self.assertEqual(observe(watch,{**c,'is_trial':True})['status'],'NO_LIVE_QUOTE')
        self.assertEqual(observe({**watch,'trigger_kind':'BREAK_ABOVE_HIGH'},c)['status'],'WAITING_FOR_CONDITION')

    def test_rotation_expands_coverage_without_breaking_liquidity_or_industry_limits(self):
        daily,companies=fixtures();p=settings()
        rows=normalize(daily,companies,date(2026,11,3))
        first,total=screen(rows,self.account,self.p,p,rotation=0)
        second,_=screen(rows,self.account,self.p,p,rotation=1)
        self.assertEqual(total,50)
        self.assertEqual(len(first),40);self.assertEqual(len(second),40)
        self.assertGreater(len({r['symbol'] for r in first+second}),40)
        self.assertEqual({r['symbol'] for r in first[:20]},{r['symbol'] for r in second[:20]})

    def test_due_scout_receives_prior_real_entry_rejections_and_cached_review_keeps_rotation(self):
        daily,companies=fixtures();at=self.at.replace(day=4)
        fetch=Mock(side_effect=lambda url:daily if url.endswith('STOCK_DAY_ALL') else companies)
        runner=Mock(side_effect=answer)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=root/'run';run.mkdir()
            first=discover(root,run,self.account,self.p,at=at,runner=runner,fetcher=fetch)
            self.assertEqual(first['status'],'COMPLETE',first)
            c=refresh(declining(first['symbols'][0]),self.account,self.rules,self.p)
            record_entry_feedback(root,[c],at)
            cached=discover(root,run,self.account,self.p,at=at+timedelta(minutes=30),runner=runner,fetcher=fetch)
            self.assertEqual(cached['screen_rotation'],first['screen_rotation'])
            self.assertEqual(cached['symbols'],first['symbols'])
            second=discover(root,run,self.account,self.p,at=at+timedelta(hours=1),runner=runner,fetcher=fetch)
            self.assertEqual(second['status'],'COMPLETE',second)
            packet=runner.call_args.args[0]
            self.assertEqual(packet['recent_entry_feedback'][0]['symbol'],c['symbol'])
            self.assertIn('NO_TREND_OR_RECOVERY_SETUP',packet['recent_entry_feedback'][0]['blockers'])
            self.assertNotEqual(first['screen_symbols'],second['screen_symbols'])

    def test_broker_or_model_failure_is_never_reported_as_no_market_opportunity(self):
        args=dict(batch=dict(quotes=[candidate()]),ranked=[],pool=[],selected=[],search={},
                  discovery=dict(status='COMPLETE'),response=dict(validation='NOT_CALLED'),
                  decision=dict(decisions=[]),account=self.account)
        self.assertEqual(summarize(**args)['outcome'],'NO_ENTRY_UNDER_CURRENT_RULES')
        self.assertEqual(summarize(**{**args,'account':{**self.account,'budget_status':'BLOCKED'}})['outcome'],
                         'ACCOUNT_OR_BROKER_BLOCKED')
        self.assertEqual(summarize(**{**args,'discovery':dict(status='UNAVAILABLE')})['outcome'],
                         'MARKET_DISCOVERY_INCOMPLETE')
        self.assertEqual(summarize(**{**args,'response':dict(validation='ERROR')})['outcome'],
                         'DECISION_RESEARCH_FAILED')


if __name__=='__main__':unittest.main()
