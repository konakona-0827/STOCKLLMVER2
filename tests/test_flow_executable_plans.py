"""Exercise agreed live plans through research, pretrade and the existing bridge."""
from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from opportunity_flow.common import stamp
from opportunity_flow.data import validate_quote,merge_research_quotes
from opportunity_flow.execution_bridge import validate_buy
from opportunity_flow.pipeline import execution_readiness,run_cycle
from opportunity_flow.recovery import runtime_code_matches
import test_flow_execution_bridge as bridge_fixture
from test_opportunity_flow import FakeRunner


class ExecutablePlanTests(unittest.TestCase):
    def setUp(self):
        fixture=bridge_fixture.BridgeTests('test_approved_research_reaches_existing_interface_without_chasing')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.f=fixture
        self.quote={**fixture.quote,'last_price':100.0,'bid':99.9,'ask':100.0,
                    'open':95.0,'high':101.0,'low':94.0}
        self.context={**fixture.context,'research_support_policy':'advisory','pricing_policy':'AT_QUOTE'}
        self.supports=[dict(price=price,date=fixture.at[:10],known_on=fixture.at[:10],kind='CONFIRMED_SWING_LOW')
                       for price in (95.0,90.0)]
        self.targets=[dict(price=120.0,date=fixture.at[:10],known_on=fixture.at[:10],kind='OBSERVED_20D_HIGH')]

    def research(self,runner=None):
        with patch('opportunity_flow.engine.levels',return_value=(self.supports,self.targets)):
            result=run_cycle(self.f.ds,self.f.at,self.f.p,self.f.root/'executable-research',
                quotes={('TWSE','1111'):self.quote},events=self.f.runtime.disclosures,
                account_context={'1111':self.context},runner=runner or FakeRunner(),use_llm=True,
                review_symbols={'1111'})
        return result,next(c for c in result['candidates'] if c['symbol']=='1111')

    def prepare_runtime(self):
        self.f.runtime.quote=deepcopy(self.quote)
        self.f.runtime.context=deepcopy(self.context)

    def test_replan_below_breached_support_reaches_pretrade_and_handoff(self):
        result,candidate=self.research()
        self.assertEqual(candidate['state'],'RESEARCH_READY')
        self.assertEqual(candidate['execution']['state'],'EXECUTION_REVIEW_READY')
        self.assertEqual(candidate['execution']['missing'],[])
        route=next(r for r in candidate['packet']['evidence'][2]['facts'] if r['route_id']==candidate['selected_route_id'])
        self.assertEqual(route['stop']['price'],90)
        self.assertLessEqual(route['economics']['modeled_loss_twd'],50)
        self.assertGreaterEqual(route['economics']['net_profit_twd'],50)
        intraday=next(e['facts'] for e in candidate['packet']['evidence'] if e['evidence_id']=='E_INTRADAY')
        self.assertEqual(intraday['low'],94)
        self.assertEqual(intraday['bid'],99.9)
        self.prepare_runtime()
        report=self.f.bridge.step(result,self.f.ds,self.f.now)
        self.assertEqual(report['blocked'],[])
        self.assertEqual(len(self.f.runtime.sent),1)
        proposal=self.f.runtime.sent[0]['recommendations'][0]
        self.assertEqual(proposal['invalidation_price'],90)
        self.assertEqual(proposal['suggested_qty'],route['economics']['quantity'])

    def test_new_low_during_pretrade_invalidates_the_frozen_plan(self):
        result,_=self.research();self.prepare_runtime()
        original=self.f.runtime.pretrade

        def review(decisions,candidates):
            self.f.runtime.quote['low']=89
            return original(decisions,candidates)

        self.f.runtime.pretrade=review
        report=self.f.bridge.step(result,self.f.ds,self.f.now)
        self.assertEqual(report['blocked'][0]['reason'],'REVIEWED_PLAN_INVALIDATED_THIS_SESSION')
        self.assertEqual(report['blocked'][0]['quote_evidence']['low'],89)
        self.assertFalse(self.f.runtime.sent)

    def test_replanned_trade_uses_original_executor_fill_ledger_and_duplicate_guard(self):
        result,_=self.research();self.prepare_runtime()
        self.f.result=result
        self.f.check_existing_executor_partial_fill('30000','30000')

    def test_current_planned_mode_and_4080_budget_reach_original_sdk_at_frozen_limit(self):
        self.context.update(pricing_policy='PLANNED_LIMIT',available_cash_twd=4080)
        result,candidate=self.research();self.prepare_runtime()
        self.f.runtime.config.planned_limits_enabled=True
        self.f.runtime.snapshot['current_buy_budget_twd']=4080
        self.f.runtime.quote.update(last_price=100.5,bid=100.0,ask=100.5)
        self.f.result=result
        # Use the real current reserve/cash relation and original executor's
        # enabled planned-price guard, not just the bridge's fake sender.
        self.f.check_existing_executor_partial_fill('9080','9080',planned_limits_enabled=True)
        recommendation=self.f.runtime.sent[0]['recommendations'][0]
        self.assertEqual(recommendation['suggested_price'],100)
        self.assertEqual(recommendation['entry_plan']['limit_price'],100)
        self.assertEqual(recommendation['pricing_policy'],'PLANNED_LIMIT')
        self.assertEqual(candidate['execution']['missing'],[])

    def test_all_breached_stops_are_blocked_before_model_review(self):
        self.quote['low']=89
        runner=FakeRunner();_,candidate=self.research(runner)
        self.assertEqual(candidate['state'],'ECONOMIC_BLOCKED')
        self.assertIn('SESSION_STRUCTURAL_STOP_BREACHED',candidate['reasons'])
        self.assertFalse(runner.calls)

    def test_advisory_readiness_and_execution_share_the_session_low_check(self):
        _,candidate=self.research()
        route_id=candidate['selected_route_id'];packet=candidate['review']['packet']
        q={**candidate['quote'],'low':89}
        readiness=execution_readiness({**candidate,'quote':q},packet,route_id,self.f.now,
            self.f.runtime.disclosures['1111'],self.f.p,support_policy='advisory')
        self.assertEqual(readiness['state'],'INVALIDATED')
        with self.assertRaisesRegex(ValueError,'INVALIDATED_THIS_SESSION'):
            validate_buy(candidate,{**self.quote,'low':89},self.f.runtime.snapshot,self.context,
                         self.f.p,self.f.now,current_event=self.f.runtime.disclosures['1111'])

    def test_planned_limit_above_entry_is_ready_at_the_original_limit(self):
        self.context['pricing_policy']='PLANNED_LIMIT'
        result,candidate=self.research();self.prepare_runtime()
        self.f.runtime.config.planned_limits_enabled=True
        self.f.runtime.quote['ask']=100.1
        ready=execution_readiness({**candidate,'quote':{**candidate['quote'],'ask':100.1}},
            candidate['review']['packet'],candidate['selected_route_id'],self.f.now,
            self.f.runtime.disclosures['1111'],self.f.p,support_policy='advisory',planned_limits=True)
        self.assertEqual(ready['state'],'EXECUTION_REVIEW_READY')
        report=self.f.bridge.step(result,self.f.ds,self.f.now)
        self.assertEqual(report['blocked'],[])
        self.assertEqual(self.f.runtime.sent[0]['recommendations'][0]['suggested_price'],100)

    def test_broker_range_is_validated_and_preserved(self):
        q,error=validate_quote(self.quote,'1111','TWSE',self.f.at,120)
        self.assertIsNone(error)
        self.assertEqual((q['low'],q['high'],q['open']),(94,101,95))
        q,error=validate_quote({**self.quote,'low':102},'1111','TWSE',self.f.at,120)
        self.assertIsNone(q)
        self.assertEqual(error,'QUOTE_SESSION_RANGE_INVALID')

    def test_formal_plan_waits_for_session_low_before_using_the_model(self):
        self.quote.pop('low')
        runner=FakeRunner();_,candidate=self.research(runner)
        self.assertEqual(candidate['state'],'REVIEW_DEFERRED_LIVE_QUOTE')
        self.assertFalse(runner.calls)

    def test_partial_broker_quote_preserves_public_screening_but_still_defers_llm(self):
        partial={**self.quote,'ask':None}
        public={**self.quote,'quote_status':'PUBLIC_REFERENCE','quote_basis':'TWSE_MIS_BOARD_LOT_NOT_ODDLOT','source':'TWSE_MIS'}
        merged=merge_research_quotes({('TWSE','1111'):public},{'1111':partial},self.f.at,120)
        self.quote=merged[('TWSE','1111')]
        self.assertEqual(self.quote['source'],'TWSE_MIS')
        self.assertEqual(self.quote['broker_quote_problem'],'ODDLOT_ASK_MISSING')
        runner=FakeRunner();_,candidate=self.research(runner)
        self.assertEqual(candidate['state'],'REVIEW_DEFERRED_LIVE_QUOTE')
        self.assertIsNone(candidate['quote'])
        self.assertEqual(candidate['packet']['evidence'][-1]['facts']['quote_basis'],'PUBLIC_REFERENCE')
        self.assertFalse(runner.calls)

    def test_complete_broker_quote_replaces_reference_and_stale_reference_is_not_used(self):
        public={**self.quote,'quote_status':'PUBLIC_REFERENCE','quote_basis':'TWSE_MIS_BOARD_LOT_NOT_ODDLOT','source':'TWSE_MIS'}
        key=('TWSE','1111')
        merged=merge_research_quotes({key:public},{'1111':self.quote},self.f.at,120)
        self.assertEqual(merged[key]['source'],'CAPITAL_SKCOM')
        public['quote_timestamp']='2026-01-01T09:00:00+08:00'
        partial={**self.quote,'ask':None}
        merged=merge_research_quotes({key:public},{'1111':partial},self.f.at,120)
        self.assertEqual(merged[key],partial)

    def test_pretrade_veto_reason_is_saved_without_its_request(self):
        result,_=self.research();self.prepare_runtime()

        def veto(decisions,candidates):
            decisions=deepcopy(decisions);decisions[0].update(decision='HOLD',reason='wait for liquidity')
            return decisions,dict(status='VALID',request={'private':'do not archive'},reviews=[
                dict(symbol='1111',approved=False,reason='insufficient executable book')])

        self.f.runtime.pretrade=veto
        block=self.f.bridge.step(result,self.f.ds,self.f.now)['blocked'][0]
        self.assertEqual(block['pretrade_reviews'][0]['reason'],'insufficient executable book')
        self.assertNotIn('request',block)
        self.assertFalse(self.f.runtime.sent)


class QuoteCompletionTests(unittest.TestCase):
    def test_buy_waits_for_book_and_session_low_after_partial_live_callback(self):
        from capital_multi_quote import MultiCapitalMarket
        market=MultiCapitalMarket.__new__(MultiCapitalMarket)
        market.subscribed=('1111',);market.pending_indices=set();market.callback_times={}
        market.stopped=lambda:False;market.event=Mock();market.flow_require_complete_buy=True
        sdk=Mock();market.objects={'SKQuoteLib':sdk};market.sk=SimpleNamespace(SKSTOCKLONG=Mock())
        at=stamp('2026-11-24T10:00:00+08:00')
        partial=dict(symbol='1111',source='CAPITAL_SKCOM',exchange_time=at.isoformat(),price=100,bid=99.9,ask=None,is_trial=False)
        complete={**partial,'ask':100,'low':99,'high':101}
        pumps=[]
        def pump(delay):pumps.append(delay);market.pending_indices.add((5,1))
        market.pump=pump
        with patch('capital_multi_quote.normalize_stock',side_effect=[partial,complete]),\
             patch('capital_multi_quote.read_stock'),patch('capital_multi_quote.now',return_value=at):
            quotes,errors=market._fetch_one_batch(('1111',),timeout=1,batch_index=1,batch_count=1)
        self.assertEqual(len(pumps),2)
        self.assertEqual((quotes['1111']['ask'],quotes['1111']['low']),(100,99))
        self.assertEqual(errors,{})
        sdk.SKQuoteLib_GetStockByMarketAndNo.assert_not_called()

    def test_strict_readiness_rejects_trial_missing_low_and_stale_but_sell_can_use_bid(self):
        from capital_multi_quote import buy_quote_complete
        from main import snapshot
        at=stamp('2026-11-24T10:00:00+08:00')
        q=dict(symbol='1111',source='CAPITAL_SKCOM',exchange_time=at.isoformat(),price=100,bid=99.9,ask=100,is_trial=False,low=99)
        self.assertTrue(buy_quote_complete(q,at))
        for change in [dict(ask=None),dict(low=None),dict(is_trial=True),dict(exchange_time='2026-11-24T09:00:00+08:00')]:
            self.assertFalse(buy_quote_complete({**q,**change},at))
        self.assertEqual(snapshot('1111',{**q,'ask':None},at=at)['data_quality'],'LIVE')

    def test_broker_buy_flag_is_forwarded_without_changing_default_quote_contract(self):
        from execution.broker_runtime import BrokerRuntime
        runtime=BrokerRuntime.__new__(BrokerRuntime);runtime._submit=Mock(return_value=({},{}))
        runtime.fetch_quotes_with_markets(['2330'],{'2330':5},None,lambda:False)
        self.assertEqual(runtime._submit.call_args.args[-1],5)
        runtime.fetch_quotes_with_markets(['2330'],{'2330':5},None,lambda:False,require_complete_buy=True)
        self.assertEqual(runtime._submit.call_args.args[-2:],(5,True))

    def test_empty_first_callback_waits_for_a_valid_second_update(self):
        from capital_multi_quote import MultiCapitalMarket
        market=MultiCapitalMarket.__new__(MultiCapitalMarket)
        market.subscribed=('1111',);market.pending_indices=set();market.callback_times={}
        market.stopped=lambda:False;market.event=Mock()
        sdk=Mock();market.objects={'SKQuoteLib':sdk};market.sk=SimpleNamespace(SKSTOCKLONG=Mock())
        at=stamp('2026-11-23T10:00:00+08:00')
        empty=dict(symbol='1111',source='CAPITAL_SKCOM',exchange_time=at.isoformat(),price=None,bid=None,ask=None)
        complete={**empty,'price':100,'bid':99.9,'ask':100,'is_trial':False}
        counter=[]

        def pump(delay):
            counter.append(delay);market.pending_indices.add((5,1))

        market.pump=pump
        with patch('capital_multi_quote.normalize_stock',side_effect=[empty,complete]),\
             patch('capital_multi_quote.read_stock'),patch('capital_multi_quote.now',return_value=at):
            quotes,errors=market._fetch_one_batch(('1111',),timeout=1,batch_index=1,batch_count=1)
        self.assertEqual(len(counter),2)
        self.assertEqual(quotes['1111']['ask'],100)
        self.assertEqual(errors,{})
        sdk.SKQuoteLib_GetStockByMarketAndNo.assert_not_called()


class RuntimeRevisionTests(unittest.TestCase):
    def test_report_commits_match_but_runtime_changes_require_new_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)

            def git(*args):
                return subprocess.check_output(['git',*args],cwd=root,stderr=subprocess.DEVNULL).decode().strip()

            def commit(message):
                git('add','.')
                git('-c','user.name=FLOW Test','-c','user.email=flow@example.invalid','commit','-qm',message)
                return git('rev-parse','HEAD')

            git('init','-q')
            (root/'runtime.py').write_text('version = 1\n',encoding='utf-8')
            original=commit('original code')
            (root/'reports').mkdir()
            (root/'reports'/'audit.txt').write_text('verified\n',encoding='utf-8')
            report=commit('runtime report')
            self.assertTrue(runtime_code_matches(original,report,root))
            (root/'runtime.py').write_text('version = 2\n',encoding='utf-8')
            changed=commit('changed runtime')
            self.assertFalse(runtime_code_matches(original,changed,root))
            self.assertFalse(runtime_code_matches('0'*40,changed,root))


if __name__=='__main__':unittest.main()
