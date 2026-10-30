"""Invented OHLC scenarios for typed scouting and conservative outcome replay."""
from dataclasses import replace
from datetime import date,timedelta
from copy import deepcopy
import unittest

from support_scout.contracts import Bar,Series,timestamp,DataError
from support_scout.patterns import load_registry
from support_scout.short_cycle import detect_short_cycle,ShortCycleParameters
from support_scout.opportunity import load_policy,_reference_plan,validate_view,blocking_public_gaps
from support_scout.short_eval import replay_trade
from support_scout.communication import review_wire
from support_scout.pipeline import run
from support_scout.japan_demo import demo_bundle


def series():
    rows=[];day=date(2026,5,1)
    while len(rows)<80:
        if day.weekday()<5:
            c=95+len(rows)*.1
            rows.append(Bar(day.isoformat(),day.isoformat()+'T15:30:00+09:00',day.isoformat()+'T16:00:00+09:00',c,c+1,c-1,c,10000000))
        day+=timedelta(days=1)
    return Series('9001.T','人造案例','TSE','TEST_ONLY','raw_unverified',tuple(rows),{'volume_comparable':True,'currency':'JPY'})


def definition(kind):
    return next(x for x in load_registry()['definitions'] if x['handler']=='short_cycle' and x['parameters']['setup_kind']==kind)


def momentum():
    s=series();b=list(s.bars)
    b[-3]=replace(b[-3],open=117,high=119,low=116,close=118)
    b[-2]=replace(b[-2],open=118,high=120,low=117,close=119)
    b[-1]=replace(b[-1],open=119,high=121,low=118,close=120)
    return replace(s,bars=tuple(b))


class ShortCycleTests(unittest.TestCase):
    def test_momentum_is_independent_of_old_support_anchor(self):
        s=momentum();p=detect_short_cycle(s,timestamp(s.bars[-1].available_at),definition('MOMENTUM_CONTINUATION'),{})
        self.assertTrue(p['research_candidate']);self.assertEqual(p['buy_type'],'MOMENTUM_CONTINUATION')
        self.assertIn('TACTICAL_STOP_INSIDE_ATR_NORMAL_NOISE',p['reason_codes'])

    def test_conditional_trigger_is_not_a_claim_of_existing_breakout(self):
        s=series();b=list(s.bars);b[-1]=replace(b[-1],open=103,high=108,low=102,close=104)
        s=replace(s,bars=tuple(b));p=detect_short_cycle(s,timestamp(b[-1].available_at),definition('RECLAIM_BREAKOUT'),{})
        self.assertTrue(p['research_candidate']);self.assertEqual(p['stage'],'SHORT_RECLAIM_TRIGGER')
        self.assertGreater(p['metrics']['entry_trigger_reference'],b[-1].close)

    def test_high_volatility_can_be_watch_without_confirmed_direction(self):
        s=series();b=list(s.bars)
        b[-3]=replace(b[-3],high=110,low=100,close=105)
        b[-2]=replace(b[-2],high=106,low=98,close=102)
        b[-1]=replace(b[-1],high=104,low=97,close=101,volume=15000000)
        s=replace(s,bars=tuple(b));p=detect_short_cycle(s,timestamp(b[-1].available_at),definition('RECLAIM_BREAKOUT'),{})
        self.assertEqual(p['stage'],'VOLATILE_WATCH');self.assertFalse(p['research_candidate'])
        self.assertIsNotNone(p['metrics']['watch_trigger_reference'])

    def test_future_price_or_availability_cannot_change_signal(self):
        s=momentum();cutoff=timestamp(s.bars[-2].available_at)
        expected=detect_short_cycle(s,cutoff,definition('MOMENTUM_CONTINUATION'),{})
        b=list(s.bars);b[-1]=replace(b[-1],high=999,low=1,close=900)
        self.assertEqual(expected,detect_short_cycle(replace(s,bars=tuple(b)),cutoff,definition('MOMENTUM_CONTINUATION'),{}))

    def test_costs_are_excluded_from_selection_but_not_reported_net_return(self):
        s=momentum();p=detect_short_cycle(s,timestamp(s.bars[-1].available_at),definition('MOMENTUM_CONTINUATION'),{})
        core={'technical':{'latest_close':120},'visible_series':{'price_basis':s.price_basis,'bars':[{'session':b.session,'high':b.high} for b in s.bars]}}
        plan=_reference_plan(core,p,load_policy())
        self.assertAlmostEqual(plan['target_reference']/plan['entry_reference']-1,.02)
        self.assertAlmostEqual(plan['estimated_net_return_fraction'],.0098507462686567)
        self.assertGreater(plan['selection_reward_risk'],plan['net_reward_risk'])

    def test_another_ready_pattern_does_not_promote_an_invalid_pattern(self):
        core={'technical':{'state':'INVALIDATED'},'opportunity_policy':load_policy(),'context_reject':False,
            'patterns':[{'pattern_id':'H-002','stage':'INVALIDATED','research_candidate':False,'required_evidence_ids':['e1']},
                {'pattern_id':'H-004','stage':'SHORT_RECLAIM_READY','research_candidate':True,'required_evidence_ids':['e2']}]}
        with self.assertRaises(DataError):validate_view({'action':'PROBE_BUY_REVIEW','pattern_id':'H-002','reason':'wrong setup'},core,['e1','e2'])

    def test_future_source_gaps_block_while_missing_background_is_explicitly_soft(self):
        info={'gaps':['FINANCIAL_DISCLOSURE_NOT_RETRIEVED']}
        self.assertEqual(blocking_public_gaps(info,load_policy(),'H-004'),[])
        self.assertTrue(blocking_public_gaps(info,load_policy(),'H-002'))
        self.assertTrue(blocking_public_gaps({'gaps':['PUBLICATION_AFTER_CUTOFF_OR_SAME_DAY_TIME_UNKNOWN']},load_policy(),'H-004'))

    def test_wire_deduplicates_metrics_losslessly(self):
        b=demo_bundle();b['opportunity_policy']=load_policy()
        from support_scout.communication import load_policy as wire_policy
        b['communication_policy']=wire_policy();r=run(b)
        p=r['all_packets'][0];v,aliases,_=review_wire(p)
        for pid,params in v['pattern_parameters'].items():
            if 'shared_parameter_ref' in params:
                expected=next(x['definition']['parameters'] for x in p['core']['patterns'] if x['pattern_id']==pid)
                self.assertEqual({**v[params['shared_parameter_ref']],**params['overrides']},expected)
        patterns={x['pattern_id']:x for x in v['patterns']}
        for alias,e in v['evidence'].items():
            links=e.get('values_from_pattern_metrics')
            if not links:continue
            original=next(x['values'] for x in p['core']['evidence'] if x['id']==aliases[alias])
            reconstructed=deepcopy(e['value'])
            reconstructed.update({key:patterns[links['pattern_id']]['metrics'][field] for key,field in links['fields'].items()})
            self.assertEqual(reconstructed,original)


class OutcomeTests(unittest.TestCase):
    def plan(self):
        return {'entry_ceiling_reference':101,'entry_trigger_reference':None,'stop_price_fraction':.015,
            'target_return_fraction':.02,'target_basis':'PRICE_MOVE','entry_cost_fraction':.005,'exit_cost_fraction':.005}
    def bar(self,**changes):
        b=momentum().bars[-1];return replace(b,open=100,high=101,low=99,close=100,**changes)
    def test_both_target_and_stop_same_day_uses_stop_first(self):
        b=replace(momentum().bars[-1],open=100,high=103,low=98,close=102)
        r=replay_trade(self.plan(),[b],3)
        self.assertEqual(r['status'],'AMBIGUOUS_STOP_FIRST');self.assertLess(r['cost_adjusted_return'],0)
    def test_gap_above_entry_band_is_not_a_profitable_fill(self):
        b=replace(momentum().bars[-1],open=110,high=120,low=109,close=119)
        self.assertEqual(replay_trade(self.plan(),[b],3)['status'],'NOT_FILLED_GAP_ABOVE_ENTRY_BAND')
    def test_intraday_trigger_touch_is_not_assumed_filled(self):
        p=self.plan();p['entry_trigger_reference']=100.5
        b=replace(momentum().bars[-1],open=100,high=103,low=98,close=102)
        self.assertEqual(replay_trade(p,[b],3)['status'],'INTRADAY_TRIGGER_TOUCHED_FILL_UNVERIFIED')
    def test_two_percent_price_target_is_not_two_percent_net(self):
        b=replace(momentum().bars[-1],open=100,high=102.5,low=99,close=102)
        r=replay_trade(self.plan(),[b],3)
        self.assertEqual(r['status'],'TARGET');self.assertAlmostEqual(r['gross_return'],.02)
        self.assertLess(r['cost_adjusted_return'],.02)
    def test_unfinished_horizon_does_not_invent_exit(self):
        self.assertEqual(replay_trade(self.plan(),[self.bar()],3)['status'],'INCOMPLETE_HORIZON')


class TypedConsensusTests(unittest.TestCase):
    def fixture(self):
        from tests.test_opportunity import fixture
        def add_short(core):
            p=deepcopy(core['patterns'][0])
            p.update(pattern_id='H-003',stage='SHORT_PULLBACK_READY',buy_type='TREND_PULLBACK',buy_type_label='趨勢回檔承接')
            p['support'].update(anchor=102,lower=102*.985,invalidation_basis='TACTICAL_PRICE_STOP_REPRICED_BEFORE_ENTRY_FIXED_AFTER_CONFIRMED_FILL')
            p['metrics'].update(distance_from_support_fraction=0,entry_trigger_reference=None,
                entry_ceiling_reference=102*1.015,stop_price_fraction=.015)
            core['patterns'].append(p)
        return fixture(core_mutation=add_short)

    def test_two_positive_votes_for_different_types_wait(self):
        from support_scout.opportunity import attach_opportunities
        r,b,_=self.fixture()
        r['securities'][0]['agent_reviews']['reviewer']['opportunity_view']['pattern_id']='H-003'
        o=attach_opportunities(r,b)['securities'][0]['opportunity_decision']
        self.assertEqual(o['action'],'WAIT')
        self.assertIn('ACTIVE_BUY_TYPE_DISAGREEMENT',o['reason_codes'])

    def test_agreed_short_type_uses_price_rr_and_still_sizes_actual_cost_risk(self):
        from support_scout.opportunity import attach_opportunities
        from support_scout.sizing import attach_execution_plans
        from tests.test_sizing import settings
        r,b,_=self.fixture()
        for review in r['securities'][0]['agent_reviews'].values():
            review['opportunity_view']['pattern_id']='H-003'
        attach_opportunities(r,b)
        o=r['securities'][0]['opportunity_decision']
        self.assertEqual(o['action'],'PROBE_BUY_REVIEW')
        self.assertLess(o['reference_plan']['net_reward_risk'],load_policy()['minimum_net_rr'])
        attach_execution_plans(r,settings())
        p=r['securities'][0]['execution_plan']
        self.assertEqual(p['suggested_amount_jpy'],50000)
        self.assertGreater(p['estimated_loss_jpy'],50000*.015)


if __name__=='__main__':unittest.main()
