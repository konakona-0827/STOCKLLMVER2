"""Exploratory recent-support controls; no network, orders or inferred accuracy."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import unittest

from support_scout.contracts import Bar, DataError, Series, TOKYO, timestamp
from support_scout.patterns import H002Parameters, detect_h002, load_registry


def definition():
    return next(d for d in load_registry()['definitions'] if d['id'] == 'H-002')


def fixture(*, last_low=92, last_close=93, volume=600):
    bars=[]
    for i in range(22):
        day=(datetime(2026,1,1,tzinfo=TOKYO)+timedelta(days=i)).date().isoformat()
        prices=(100,101,99,100,1000) if i < 20 else (94,96,90,95,1000)
        if i==21:prices=(94,95,last_low,last_close,volume)
        bars.append(Bar(day,day+'T15:30:00+09:00',day+'T16:00:00+09:00',*prices))
    return Series('9001.T','synthetic','TSE','fixture','raw_unverified',tuple(bars),{'volume_comparable':False})


def evaluate(series=None, *, context=None, d=None, cutoff=None):
    series=series or fixture()
    return detect_h002(series,timestamp(cutoff or series.bars[-1].available_at),d or definition(),context or {})


class ActivePatternTests(unittest.TestCase):
    def test_short_hold_is_a_research_opportunity_with_explicit_raw_data_gap(self):
        p=evaluate()
        self.assertEqual(p['stage'],'SUPPORT_HELD')
        self.assertTrue(p['research_candidate'])
        self.assertFalse(p['execution_enabled'])
        self.assertAlmostEqual(p['metrics']['volume_contraction_ratio'],.6)
        self.assertEqual(p['metrics']['no_new_low_sessions'],1)
        self.assertEqual(p['support']['anchor'],90)
        self.assertAlmostEqual(p['support']['lower'],89.1)
        self.assertIn('PATTERN_VOLUME_BASIS_UNVERIFIED',p['reason_codes'])
        self.assertIn('EXPLORATORY',p['empirical_status'])

    def test_anchor_day_alone_and_insufficient_holding_period_are_not_candidates(self):
        s=fixture()
        self.assertFalse(evaluate(replace(s,bars=s.bars[:-1]))['research_candidate'])
        d=definition();d['parameters']['holding_min_sessions']=2
        self.assertFalse(evaluate(d=d)['research_candidate'])

    def test_reclaim_can_be_distinguished_from_merely_holding(self):
        d=definition();d['parameters']['max_distance_fraction']=.08
        p=evaluate(fixture(last_close=95.6),d=d)
        self.assertEqual(p['stage'],'SUPPORT_RECLAIMED')
        self.assertTrue(p['metrics']['reclaimed_anchor_close'])

    def test_broken_frozen_support_is_rejected_without_moving_anchor(self):
        s=fixture()
        day='2026-01-23'
        broken=Bar(day,day+'T15:30:00+09:00',day+'T16:00:00+09:00',91,92,87,88,900)
        p=evaluate(replace(s,bars=(*s.bars,broken)))
        self.assertEqual(p['stage'],'INVALIDATED')
        self.assertFalse(p['research_candidate'])
        self.assertEqual(p['support']['anchor'],90)

    def test_late_extension_and_no_volume_contraction_are_not_positive(self):
        self.assertEqual(evaluate(fixture(last_close=99))['stage'],'EXTENDED')
        self.assertFalse(evaluate(fixture(last_close=99))['research_candidate'])
        p=evaluate(fixture(volume=1500))
        self.assertEqual(p['stage'],'VOLUME_REVIEW')
        self.assertFalse(p['research_candidate'])

    def test_new_hypothesis_after_failed_anchor_does_not_resurrect_old_episode(self):
        s=fixture()
        day='2026-01-23'
        broken=Bar(day,day+'T15:30:00+09:00',day+'T16:00:00+09:00',91,92,87,88,900)
        first=evaluate(replace(s,bars=(*s.bars,broken)))
        self.assertEqual(first['stage'],'INVALIDATED')
        self.assertEqual(first['support']['anchor'],90)
        day='2026-01-24'
        held=Bar(day,day+'T15:30:00+09:00',day+'T16:00:00+09:00',90,91,89,90,400)
        p=evaluate(replace(s,bars=(*s.bars,broken,held)))
        self.assertTrue(p['research_candidate'])
        self.assertEqual(p['support']['anchor'],87)
        self.assertEqual(p['support']['anchor_session'],'2026-01-23')
        self.assertEqual(p['episodes'][-2]['state'],'INVALIDATED')
        self.assertEqual(p['episodes'][-2]['anchor'],90)
        self.assertIn('NEW_HYPOTHESIS_DOES_NOT_REPAIR_PREVIOUS_FAILED_EPISODE',p['reason_codes'])
        old_failure=next(e for e in p['evidence'] if e['kind']=='INVALIDATED')
        self.assertEqual(old_failure['values']['anchor'],90)
        self.assertEqual(old_failure['values']['scope'],'LATEST_PREVIOUS_EPISODE_NOT_CURRENT_NEW_SUPPORT')
        self.assertIn(old_failure['id'],p['required_evidence_ids'])

    def test_future_bars_and_future_guidance_cannot_change_an_earlier_prefix(self):
        s=fixture();cutoff=s.bars[-1].available_at
        day='2026-01-23';future=Bar(day,day+'T15:30:00+09:00',day+'T16:00:00+09:00',120,150,119,140,10000)
        context={'guidance_revisions':[{'id':'future','available_at':day+'T17:00:00+09:00',
            'announced_at':day+'T16:00:00+09:00','source':'fixture','metric':'OP','unit':'JPY',
            'previous_company_guidance':100,'revised_company_guidance':140}]}
        self.assertEqual(evaluate(s),evaluate(replace(s,bars=(*s.bars,future)),context=context,cutoff=cutoff))

    def test_company_guidance_raise_is_visible_but_never_market_consensus_surprise(self):
        s=fixture();known=s.bars[-1].available_at
        context={'guidance_revisions':[{'id':'guidance','available_at':known,'announced_at':known,
            'source':'fixture','metric':'OP','unit':'JPY','previous_company_guidance':100,
            'revised_company_guidance':120,'verified':False}]}
        p=evaluate(s,context=context)
        self.assertTrue(p['metrics']['company_guidance_raise_evidence_ids'])
        event=next(e for e in p['evidence'] if e['kind']=='H002_GUIDANCE')['values']
        self.assertEqual(event['comparison_basis'],'PREVIOUS_COMPANY_GUIDANCE_NOT_MARKET_CONSENSUS')
        self.assertFalse(event['positive_surprise'])
        self.assertIsNone(event['market_consensus'])
        self.assertIn('COMPANY_GUIDANCE_EXTRACTION_UNVERIFIED',p['reason_codes'])
        context['guidance_revisions'].append(deepcopy(context['guidance_revisions'][0]))
        duplicate=evaluate(s,context=context)
        self.assertEqual(duplicate['metrics']['company_guidance_raise_evidence_ids'],[])
        self.assertIn('GUIDANCE_INPUT_INVALID',duplicate['reason_codes'])

    def test_verified_fundamental_deterioration_is_counterevidence(self):
        s=fixture()
        p=evaluate(s,context={'decline_driver':{'available_at':s.bars[-1].available_at,
            'source':'fixture','verified':True,'classification':'FUNDAMENTAL_DETERIORATION'}})
        self.assertEqual(p['stage'],'CONTRADICTED')
        self.assertFalse(p['research_candidate'])

    def test_handler_parameters_are_distinct_and_registry_validates_both(self):
        d=definition()
        self.assertEqual(d['parameters'],asdict(H002Parameters()))
        self.assertNotIn('min_support_tests',d['parameters'])
        registry=load_registry();copy=deepcopy(registry)
        next(x for x in copy['definitions'] if x['id']=='H-002')['parameters']['min_support_tests']=2
        with self.assertRaises(DataError):load_registry(copy)
        for kwargs in ({'holding_min_sessions':True},{'volume_contraction_ratio':float('nan')},
                       {'holding_min_sessions':12},{'max_distance_fraction':.02}):
            with self.subTest(kwargs=kwargs),self.assertRaises(DataError):H002Parameters(**kwargs)


if __name__=='__main__':unittest.main()
