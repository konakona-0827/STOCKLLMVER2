from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from operations.evaluate_opportunities import STAGES, apply_labels, audit, cycle_events, score_events


class OpportunityEvaluationTests(unittest.TestCase):
    def event(self, identity, success, caught, reason=None):
        return dict(event_id=identity,decision_at='2026-11-03T10:00:00+08:00',reason=reason,
            stages={stage:caught for stage in STAGES},outcome=dict(
                status='SUCCESS' if success else 'FAILURE',criterion_id='original-plan-target-first-v1',
                plan_available_at='2026-11-03T09:59:00+08:00',available_at='2026-11-04T13:30:00+08:00',
                evidence_refs=['frozen-plan','later-verified-market-path']))

    def test_precision_recall_and_f1_penalize_wrong_picks_and_misses(self):
        events=[self.event('hit',True,True),self.event('miss',True,False),
                self.event('wrong',False,True),self.event('avoided',False,False)]
        report=score_events(events)
        for stage in STAGES:
            row=report['stages'][stage]
            self.assertEqual([row[k] for k in ('TP','FP','FN','TN')],[1,1,1,1])
            self.assertEqual(row['precision'],.5);self.assertEqual(row['recall'],.5)
            self.assertEqual(row['miss_rate'],.5);self.assertEqual(row['f1'],.5)
            self.assertEqual(row['error_count'],2)
            self.assertEqual(row['missed_event_ids'],['miss'])

    def test_budget_pause_and_a_justified_veto_do_not_erase_a_positive_miss(self):
        events=[self.event('capacity',True,False,'BUDGET_PAUSE'),
                self.event('veto',True,False,'INDEPENDENT_RESEARCH_VETO')]
        report=score_events(events)['stages']['filled']
        self.assertEqual(report['FN'],2);self.assertEqual(report['recall'],0)
        self.assertEqual(report['miss_rate'],1)

    def test_no_label_never_becomes_a_zero_or_perfect_live_hit_rate(self):
        event=self.event('unknown',True,False);event['outcome']={'status':'UNAVAILABLE'}
        report=score_events([event])
        self.assertEqual(report['labelled'],0);self.assertEqual(report['label_coverage'],0)
        self.assertIsNone(report['stages']['filled']['precision'])
        self.assertIsNone(report['stages']['filled']['recall'])
        self.assertIsNone(report['whole_market_recall'])

    def test_pending_ambiguous_and_missing_outcomes_are_visible(self):
        events=[]
        for status in ('PENDING','AMBIGUOUS','UNAVAILABLE'):
            event=self.event(status,True,False);event['outcome']={'status':status};events.append(event)
        report=score_events(events)
        self.assertEqual(report['unscored'],{'PENDING':1,'AMBIGUOUS':1,'UNAVAILABLE':1})

    def test_missing_data_does_not_hide_a_process_error(self):
        event=self.event('vague',True,False);event['outcome']={'status':'UNAVAILABLE'};event['quality_error']=True
        report=score_events([event])
        self.assertEqual(report['quality_error_count'],1)
        self.assertEqual(report['quality_error_event_ids'],['vague'])

    def test_future_plan_labels_and_duplicate_events_are_rejected(self):
        event=self.event('bad',True,True)
        event['outcome']['plan_available_at']='2026-11-04T10:00:00+08:00'
        with self.assertRaisesRegex(ValueError,'CHRONOLOGICAL'):score_events([event])
        event=self.event('duplicate',True,True)
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):score_events([event,deepcopy(event)])

    def test_a_future_outcome_is_pending_until_it_is_observable(self):
        event=self.event('not-yet-known',True,True)
        report=score_events([event],as_of=datetime(2026,11,3,12,tzinfo=timezone.utc))
        self.assertEqual(report['labelled'],0)
        self.assertEqual(report['unscored'],{'PENDING':1})

    def test_unseen_benchmark_opportunity_is_counted_as_a_miss_at_every_stage(self):
        event=self.event('outside',True,False)
        label=dict(event['outcome'],event_id='outside',symbol='1101',decision_at=event['decision_at'])
        events=apply_labels([],dict(criterion_id=label['criterion_id'],labels=[label]))
        report=score_events(events)
        self.assertTrue(events[0]['external_benchmark'])
        self.assertTrue(all(report['stages'][stage]['FN']==1 for stage in STAGES))

    def test_selection_success_and_execution_miss_are_separate(self):
        event=self.event('stopped-later',True,True)
        event['stages'].update(submitted=False,filled=False)
        report=score_events([event])
        self.assertEqual(report['stages']['buy']['TP'],1)
        self.assertEqual(report['stages']['filled']['FN'],1)

    def folder(self, root, decision='WAIT'):
        folder=Path(root)/'data/analysis/runs/R1';folder.mkdir(parents=True)
        records={'run.json':dict(run_id='R1',status='COMPLETE',ai_validation='VALID',
                                 started_at='2026-11-03T10:00:00+08:00'),
                 'opportunity_pool_after_selection.json':[dict(symbol='1101',trade_setups=[dict(setup_id='S1')])],
                 'analysis_set.json':[dict(symbol='1101',trade_setups=[dict(setup_id='S1')])],
                 'selection_decision.json':dict(decisions=[dict(symbol='1101',decision=decision,reason='尚未證明會上漲')])}
        for name,record in records.items():(folder/name).write_text(json.dumps(record),encoding='utf-8')
        return folder

    def test_qualified_wait_without_verifiable_counter_evidence_is_an_explicit_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=self.folder(tmp)
            event=cycle_events(folder)[0]
            self.assertTrue(event['quality_error'])
            self.assertEqual(event['reason'],'UNVERIFIED_QUALIFIED_NON_BUY_REASON')
            self.assertEqual(audit(tmp)['evaluation']['quality_error_count'],1)

    def test_selling_an_existing_holding_is_not_a_captured_entry_opportunity(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=self.folder(tmp,decision='SELL')
            (folder/'execution_report.json').write_text(json.dumps(dict(results=[
                dict(symbol='1101',side='SELL',broker_order_sent=True,filled_quantity=10)])),encoding='utf-8')
            event=cycle_events(folder)[0]
            self.assertFalse(event['stages']['submitted']);self.assertFalse(event['stages']['filled'])

    def test_agent_versions_are_not_silently_mixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.folder(tmp)
            report=audit(tmp,agent_version='new-agent-v9')
            self.assertEqual(report['archive']['runs'],0)
            self.assertIsNone(report['evaluation']['stages']['buy']['precision'])
