from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from operations.expanded_opportunity_replay import save,score_variant,route_outcome


class ExpandedReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.setup=dict(setup_id='route',strategy_id='intraday_recovery',holding_days=2,
            entry=100,target=110,invalidation=95,max_qty=10)
        self.b=dict(events=[dict(event_id='day|X',symbol='X',split='validation',source_run='g')])
        self.labels=dict(labels={'day|X':dict(reference='SUCCESS',routes={
            'route':dict(status='SUCCESS')})})

    def write_result(self,buy=False,selected=True,plans=True,validation='VALID'):
        d=dict(symbol='X',decision='BUY' if buy else 'WAIT',strategy_id='intraday_recovery',
            holding_days=2,suggested_price=100,target_price=110,invalidation_price=95,suggested_qty=5,
            reason='specific reason')
        save(self.root/'g/result.json',dict(group=dict(group_id='g',events=['day|X']),
            selected_symbols=['X'] if selected else [],all_candidates=[dict(symbol='X',
                trade_setups=[self.setup] if plans else [])],decision=dict(decisions=[d]),
            response=dict(validation=validation)))

    def test_all_wait_and_missing_results_count_positive_misses(self):
        incomplete=score_variant(self.b,self.labels,self.root)['overall']
        self.assertEqual((incomplete['misses'],incomplete['incomplete_replays']),(1,1))
        self.write_result()
        wait=score_variant(self.b,self.labels,self.root)['overall']
        self.assertEqual((wait['capture_recall'],wait['misses']),(0,1))
        self.assertIsNone(wait['pick_precision'])

    def test_filtering_positive_case_does_not_shrink_denominator(self):
        for selected,plans,reason in [(False,True,'SHORTLIST_OMISSION'),(False,False,'STRATEGY_FILTER')]:
            self.write_result(selected=selected,plans=plans)
            m=score_variant(self.b,self.labels,self.root)['overall']
            self.assertEqual((m['events'],m['reference_positive'],m['misses']),(1,1,1))
            self.assertEqual(m['miss_causes'],{reason:1})

    def test_wrong_route_and_missed_positive_are_both_errors_with_unique_event_count(self):
        self.write_result(buy=True);self.labels['labels']['day|X']['routes']['route']['status']='FAILURE'
        m=score_variant(self.b,self.labels,self.root)['overall']
        self.assertEqual((m['wrong_picks'],m['misses'],m['error_count'],m['unique_error_events']),(1,1,2,1))
        self.assertEqual(m['pick_precision'],0)

    def test_unknown_entry_does_not_earn_success_credit(self):
        self.write_result(buy=True);self.labels['labels']['day|X']['routes']['route']['status']='UNAVAILABLE'
        m=score_variant(self.b,self.labels,self.root)['overall']
        self.assertEqual((m['successful_picks'],m['misses'],m['unscored_buys']),(0,1,1))
        self.assertIsNone(m['pick_precision'])

    def test_invalid_model_output_cannot_earn_capture_credit(self):
        self.write_result(buy=True,validation='ERROR')
        m=score_variant(self.b,self.labels,self.root)['overall']
        self.assertEqual((m['successful_picks'],m['misses']),(0,1))

    def test_successful_alternative_route_counts_as_opportunity_even_when_reference_lost(self):
        self.labels['labels']['day|X']['reference']='FAILURE'
        self.write_result()
        m=score_variant(self.b,self.labels,self.root)['overall']
        self.assertEqual((m['reference_positive'],m['opportunity_positive'],m['misses']),(0,1,1))

    def test_pretrade_veto_is_distinct_from_agent_wait(self):
        self.write_result()
        path=self.root/'g/result.json'
        from operations.expanded_opportunity_replay import read
        r=read(path);r['decision']['decisions'][0]['warnings']=['PRETRADE_REVIEW_VETO'];save(path,r)
        m=score_variant(self.b,self.labels,self.root)['overall']
        self.assertEqual(m['miss_causes'],{'PRETRADE_VETO':1})

    def test_results_from_another_benchmark_are_rejected(self):
        self.write_result();self.b['benchmark_sha256']='different'
        with self.assertRaisesRegex(ValueError,'REPLAY_BENCHMARK_MISMATCH'):
            score_variant(self.b,self.labels,self.root)

    def test_known_post_entry_stop_is_not_erased_by_skipping_full_entry_day(self):
        c=dict(quote_timestamp='2026-10-19T11:00:00+08:00',ask=100,quote_status='LIVE',is_trial=False)
        quotes=[dict(quote_timestamp='2026-10-19T12:00:00+08:00',last_price=94,quote_status='LIVE')]
        bars=[dict(date='2026-10-19',low=94),dict(date='2026-10-20',open=111,high=112,low=109,close=111)]
        r=route_outcome(c,self.setup,quotes,bars)
        self.assertEqual(r['status'],'FAILURE')
        self.assertEqual(r['daily_price_outcome']['reason'],'POST_ENTRY_SAME_DAY_INVALIDATION')

    def test_unknown_entry_day_stop_order_cannot_be_credited_as_success(self):
        c=dict(quote_timestamp='2026-10-19T11:00:00+08:00',ask=100,quote_status='LIVE',is_trial=False)
        bars=[dict(date='2026-10-19',low=94),dict(date='2026-10-20',open=111,high=112,low=109,close=111)]
        self.assertEqual(route_outcome(c,self.setup,[],bars)['status'],'AMBIGUOUS')

    def test_below_invalidation_quote_cannot_prove_limit_entry(self):
        c=dict(quote_timestamp='2026-10-19T11:00:00+08:00',ask=101,quote_status='LIVE',is_trial=False)
        quotes=[dict(quote_timestamp='2026-10-19T12:00:00+08:00',ask=94,last_price=96,quote_status='LIVE')]
        from operations.replay_archived_plans import find_quote_entry
        self.assertEqual(find_quote_entry(c,self.setup,quotes)['status'],'UNCONFIRMED_ENTRY')

    def test_wrong_buy_outside_deduplicated_cohort_is_still_reported(self):
        self.write_result()
        from operations.expanded_opportunity_replay import read
        path=self.root/'g/result.json';r=read(path)
        r['all_candidates'].append(dict(symbol='Y',trade_setups=[self.setup]))
        r['decision']['decisions'].append(dict(symbol='Y',decision='BUY',strategy_id='intraday_recovery',
            holding_days=2,suggested_price=100,target_price=110,invalidation_price=95,suggested_qty=5))
        save(path,r)
        self.labels['pool_routes']={'g':{'Y':{'route':dict(status='FAILURE')}}}
        result=score_variant(self.b,self.labels,self.root)
        self.assertEqual(result['overall']['wrong_picks'],0)
        self.assertEqual(result['all_snapshot_buys']['wrong'],1)
        self.assertEqual(result['all_snapshot_buys']['outside_first_episode_cohort'],1)

    def later_buy(self,next_day=False,changed_entry=False):
        self.write_result()
        self.b['events'][0].update(as_of='2026-10-19T11:00:00+08:00',candidate=dict(trade_setups=[self.setup]))
        from operations.expanded_opportunity_replay import read
        r=read(self.root/'g/result.json');r['group']['as_of']='2026-10-19T11:00:00+08:00';save(self.root/'g/result.json',r)
        r=deepcopy(r);r['group']=dict(group_id='later',events=[],as_of=('2026-10-20' if next_day else '2026-10-19')+'T12:00:00+08:00')
        d=r['decision']['decisions'][0];d['decision']='BUY'
        if changed_entry:
            d['suggested_price']=101;r['all_candidates'][0]['trade_setups'][0].update(entry=101,setup_id='new')
        save(self.root/'later/result.json',r)
        self.labels['pool_routes']={'later':{'X':{('new' if changed_entry else 'route'):dict(status='SUCCESS')}}}

    def test_same_day_later_exact_original_plan_is_capture_but_first_deferral_stays_visible(self):
        self.later_buy();m=score_variant(self.b,self.labels,self.root)['overall']
        self.assertEqual((m['first_response_misses'],m['late_captures'],m['same_day_original_plan_misses']),(1,1,0))

    def test_next_day_buy_cannot_retroactively_capture_expired_rod_entry(self):
        self.later_buy(next_day=True);m=score_variant(self.b,self.labels,self.root)['overall']
        self.assertEqual((m['late_captures'],m['same_day_original_plan_misses']),(0,1))

    def test_later_changed_entry_cannot_erase_original_plan_miss(self):
        self.later_buy(changed_entry=True);m=score_variant(self.b,self.labels,self.root)['overall']
        self.assertEqual((m['late_captures'],m['same_day_original_plan_misses']),(0,1))

