"""Do not mistake missing inputs, service failures or known events for no market opportunity."""
from copy import deepcopy
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import Mock,patch

from opportunity_flow.common import read
from opportunity_flow.decision_quality import summarize
from opportunity_flow.pipeline import run_cycle
import test_flow_execution_bridge as fixture_module
from test_opportunity_flow import FakeRunner


class DecisionQualityTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture_module.BridgeTests('test_approved_research_reaches_existing_interface_without_chasing')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.f=self.fixture;self.state=self.f.root/'quality'

    def cycle(self,event,runner):
        return run_cycle(self.f.ds,self.f.at,self.f.p,self.state,
            quotes={('TWSE','1111'):self.f.quote},events={'1111':event},runner=runner,use_llm=True,
            account_context={'1111':self.f.context},review_symbols={'1111'})

    def test_known_action_does_not_repeat_research_and_clear_event_automatically_resumes(self):
        event=deepcopy(self.f.runtime.disclosures['1111'])
        event['blockers']=['UPCOMING_CORPORATE_ACTION_REQUIRES_NEW_PLAN']
        runner=FakeRunner()
        first=self.cycle(event,runner)
        self.assertEqual(first['candidates'][0]['state'],'REVIEW_DEFERRED_EVENT_RISK')
        second=self.cycle(event,runner)
        self.assertFalse(runner.calls)
        self.assertEqual(second['decision_quality']['deferred_candidates'],1)
        event['blockers']=[]
        recovered=self.cycle(event,runner)
        self.assertEqual(recovered['candidates'][0]['state'],'RESEARCH_READY')
        self.assertEqual(len(runner.calls),2)
        self.assertEqual(recovered['decision_quality']['paired_research_candidates'],1)
        self.assertFalse(read(self.state/'decision_quality.json')['market_opportunity_absence_verified'])
        self.assertFalse(self.f.runtime.sent)

    def test_new_known_risk_cannot_reuse_cached_approval(self):
        event=deepcopy(self.f.runtime.disclosures['1111']);runner=FakeRunner()
        self.assertEqual(self.cycle(event,runner)['candidates'][0]['state'],'RESEARCH_READY')
        count=len(runner.calls);event['blockers']=['MATERIAL_TRADING_OR_CAPITAL_EVENT']
        c=self.cycle(event,runner)['candidates'][0]
        self.assertEqual(c['state'],'REVIEW_DEFERRED_EVENT_RISK');self.assertNotIn('review',c)
        self.assertEqual(len(runner.calls),count)

    def test_source_outage_is_not_a_market_watch_or_an_automatic_clear(self):
        event=dict(status='SOURCE_UNAVAILABLE',blockers=['TWSE_NEWS'],source_url='https://official.invalid/retained-source');runner=FakeRunner()
        result=self.cycle(event,runner)
        self.assertFalse(runner.calls)
        self.assertEqual(result['candidates'][0]['state'],'REVIEW_DEFERRED_EVENT_SOURCE')
        self.assertEqual(result['decision_quality']['deferred_candidates'],1)
        self.assertEqual(result['decision_quality']['paired_research_candidates'],0)
        self.assertEqual(result['candidates'][0]['information_availability']['current_disclosures']['origin'],'SOURCE_UNAVAILABLE')
        # The full numeric fixture has two packet-bearing stocks: one failed
        # source and one with no supplied event, although only 1111 is reviewed.
        self.assertEqual(result['decision_quality']['information_gaps']['current_disclosures:SOURCE_UNAVAILABLE'],2)

    def test_quality_preserves_error_and_data_counts_without_inventing_returns(self):
        rows=[dict(symbol='1',state='DATA_BLOCKED'),dict(symbol='2',state='REVIEW_ERROR'),
            dict(symbol='3',state='RESEARCH_WATCH',information_availability={
                'intraday_history':dict(origin='SOURCE_RESPONSE_INCOMPLETE'),
                'historical_corporate_action_adjustment':dict(origin='SYSTEM_NOT_INTEGRATED')})]
        report=summarize(rows,[dict(status='ERROR')])
        self.assertEqual(report['data_blocked'],1);self.assertEqual(report['research_errors_or_backoff'],1)
        self.assertEqual(report['information_gaps']['historical_corporate_action_adjustment:SYSTEM_NOT_INTEGRATED'],1)
        self.assertFalse(report['market_opportunity_absence_verified']);self.assertNotIn('profit',report)

    def test_runtime_close_automatically_waits_for_pending_native_cleanup(self):
        from opportunity_flow.legacy_runtime import LegacyRuntime
        runtime=LegacyRuntime.__new__(LegacyRuntime);runtime.root=self.f.root
        results=[dict(status='STOPPING_BROKER_CLEANUP_PENDING',lease_held=True),
            dict(status='STOPPING_BROKER_UNRESPONSIVE',lease_held=True),dict(status='STOPPED')]
        with patch('execution.broker_runtime.shutdown_broker_runtime',side_effect=results) as close,\
             patch('opportunity_flow.legacy_runtime.time.sleep') as pause:
            self.assertEqual(runtime.close()['status'],'STOPPED')
        self.assertEqual(close.call_count,3);self.assertEqual(pause.call_count,2)

    def paths(self,state):
        with sqlite3.connect(Path(state)/'flow.sqlite3') as db:
            return db.execute('SELECT packet_id,stage,reason,observations FROM execution_paths').fetchall()

    def test_ready_execution_result_survives_later_blocked_cycle_and_restart(self):
        self.f.bridge.step(self.f.result,self.f.ds,self.f.now)
        rows=self.paths(self.f.bridge.state)
        self.assertTrue(any(r[1]=='HANDOFF_RECORDED' for r in rows))
        self.f.runtime.quote['is_trial']=True
        self.f.bridge.step(self.f.result,self.f.ds,self.f.now)
        rows=self.paths(self.f.bridge.state)
        self.assertTrue(any(r[1]=='HANDOFF_RECORDED' for r in rows))
        self.assertTrue(any(r[1:3]==('BLOCKED','LIVE_NON_TRIAL_QUOTE_REQUIRED') for r in rows))
        self.assertEqual(len(self.f.runtime.sent),1)

    def test_offer_during_broker_failure_has_durable_reason_without_sending(self):
        from opportunity_flow.live import LiveCoordinator
        c=LiveCoordinator('unused',self.state,self.f.p,runtime=self.f.runtime)
        try:
            c.broker_read_error='unverified'
            for _ in range(3):c.offer(self.f.result,self.f.ds)
            rows=self.paths(self.state)
            self.assertTrue(any(r[1:] == ('BLOCKED','BROKER_READ_UNVERIFIED',3) for r in rows))
            self.assertFalse(self.f.runtime.sent);self.assertIsNone(c.future)
        finally:c.close()

    def test_diagnostic_write_failure_never_changes_a_completed_dispatch(self):
        with patch('opportunity_flow.execution_audit.Ledger',side_effect=OSError('storage unavailable')):
            report=self.f.bridge.step(self.f.result,self.f.ds,self.f.now)
        self.assertFalse(report['blocked']);self.assertEqual(len(self.f.runtime.sent),1)

    def test_public_path_export_retains_ready_identity_and_excludes_private_details(self):
        from opportunity_flow.execution_audit import record,public_paths
        from datetime import datetime
        from opportunity_flow.common import TAIPEI
        record(self.state,self.f.result['candidates'],'BLOCKED','private-account-detail',detail={'credential':'secret'})
        report=public_paths(self.state,datetime.now(TAIPEI).date().isoformat())
        self.assertEqual(len(report['paths']),1)
        self.assertEqual(report['paths'][0]['packet_id'],self.f.c['review']['packet']['packet_id'])
        self.assertEqual(report['paths'][0]['reason'],'DETAIL_IN_LOCAL_AUDIT')
        self.assertNotIn('secret',str(report));self.assertNotIn('private-account',str(report))

    def test_unrelated_position_error_cannot_mislabel_successful_buy_as_blocked(self):
        self.f.bridge.review_positions=Mock(side_effect=RuntimeError('unrelated position model'))
        self.f.bridge.step(self.f.result,self.f.ds,self.f.now)
        stages=[r[1] for r in self.paths(self.f.bridge.state)]
        self.assertIn('HANDOFF_RECORDED',stages);self.assertNotIn('BLOCKED',stages)
        self.assertEqual(len(self.f.runtime.sent),1)


if __name__=='__main__':unittest.main()
