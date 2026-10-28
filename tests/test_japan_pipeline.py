from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest

from support_scout.contracts import Rules, digest, timestamp
from support_scout.japan_demo import demo_bundle
from support_scout.decision import attach_decisions, DecisionPolicy
from support_scout.ledger import record_observation
from support_scout.observation import observe
from support_scout.pipeline import run


class JapanPipelineTests(unittest.TestCase):
    def test_japan_profile_and_currency_specific_liquidity(self):
        bundle = demo_bundle()
        report = run(bundle)
        item = report['securities'][0]
        self.assertEqual(report['market'], 'JP')
        self.assertEqual(item['data_status'], 'VALID')
        self.assertEqual(item['technical']['state'], 'RULE_CONFIRMED')
        self.assertIn('average_turnover_20_jpy', item['technical'])
        self.assertNotIn('average_turnover_20_twd', item['technical'])
        self.assertNotEqual(item['trade_decision']['conclusion'], 'CONSIDER')
        high_jpy = replace(Rules(), min_turnover_jpy=1e15)
        item = run(bundle, rules=high_jpy)['securities'][0]
        self.assertIn('LOW_20_SESSION_TURNOVER', item['technical']['blockers'])

    def test_japan_cannot_mix_currency_exchange_or_taiwan_closing_time(self):
        for field, value in [('currency', 'TWD'), ('exchange', 'TWSE')]:
            bundle = demo_bundle()
            bundle['securities'][0][field] = value
            self.assertEqual(run(bundle)['securities'][0]['data_status'], 'REJECTED')
        bundle = demo_bundle()
        bundle['securities'][0]['bars'][-1]['closed_at'] = bundle['calendar']['sessions'][-1] + 'T13:30:00+09:00'
        self.assertIn('INVALID_BAR_AVAILABILITY', run(bundle)['securities'][0]['reasons'])

    def test_before_publication_cutoff_excludes_today(self):
        bundle = demo_bundle()
        day = bundle['calendar']['sessions'][-1]
        bundle['market_context']['available_at'] = day + 'T15:00:00+09:00'
        bundle['securities'][0]['dossier']['available_at'] = day + 'T15:00:00+09:00'
        report = run(bundle, as_of=day + 'T15:45:00+09:00')
        self.assertEqual(report['securities'][0]['technical']['latest_session'], bundle['calendar']['sessions'][-2])

    def test_raw_provider_blockers_are_preserved_and_reviews_cannot_clear_them(self):
        bundle = demo_bundle()
        bundle['securities'][0]['observation_blockers'] = ['EQUITY_CLASSIFICATION_UNVERIFIED']
        item = run(bundle)['securities'][0]
        self.assertIn('EQUITY_CLASSIFICATION_UNVERIFIED', item['context_blockers'])
        self.assertNotEqual(item['trade_decision']['conclusion'], 'CONSIDER')

    def test_provider_failure_reason_survives_the_report(self):
        bundle = demo_bundle()
        bundle['securities'][0]['bars'] = []
        bundle['securities'][0]['collection_errors'] = ['CORPORATE_ACTION_IN_WINDOW_REQUIRES_REVIEW']
        item = run(bundle)['securities'][0]
        self.assertIn('CORPORATE_ACTION_IN_WINDOW_REQUIRES_REVIEW', item['reasons'])

    def test_future_append_does_not_change_packet_or_decision(self):
        bundle = demo_bundle()
        original = run(bundle)
        row = deepcopy(bundle['securities'][0]['bars'][-1])
        row.update(session='2027-01-04', closed_at='2027-01-04T15:30:00+09:00', available_at='2027-01-04T16:00:00+09:00')
        bundle['securities'][0]['bars'].append(row)
        replay = run(bundle)
        self.assertEqual(original['snapshot_id'], replay['snapshot_id'])
        self.assertEqual(original['securities'][0]['trade_decision'], replay['securities'][0]['trade_decision'])

    def test_daily_japan_report_has_replayable_final_decisions(self):
        bundle = demo_bundle()
        bundle.update(synthetic=False, observation_only=True, collection={'status': 'COMPLETE'})
        for item in bundle['securities']:
            item.update(observation_only=True, price_basis='raw_unverified', point_in_time=False)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report, target, code = observe(bundle=bundle, agents=False, output_root=root/'out', db_path=root/'jp.sqlite3', settings_path=root/'absent.json')
            saved = json.loads((target/'report.json').read_text(encoding='utf-8'))
            manifest = json.loads((target/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(report, saved)
            self.assertEqual(manifest['market'], 'JP')
            self.assertTrue((target/'committed.json').exists())
            self.assertEqual(report['decision_summary']['counts']['CONSIDER'], 0)
            self.assertIn('日股支撐決策', (target/'report.html').read_text(encoding='utf-8'))
            replay = run(json.loads((target/'input.json').read_text(encoding='utf-8')), rules=Rules(**report['rules']))
            self.assertEqual(replay['snapshot_id'], report['snapshot_id'])

    def test_duplicate_approved_snapshot_cannot_bypass_later_ledger_invalidation(self):
        bundle = demo_bundle()
        bundle.update(synthetic=False, collection={'status': 'COMPLETE'})
        bundle['securities'] = bundle['securities'][:1]
        initial = run(bundle)
        packet = initial['agent_packets'][0]
        reviews = [{'packet_id': packet['packet_id'], 'role': role, 'agent_id': 'test-' + role,
            'verdict': 'RESEARCH', 'evidence_ids': packet['core']['required_confirmation_evidence'],
            'summary': '僅供測試', 'risks': [], 'missing_data': [], 'produced_at': bundle['as_of']}
            for role in ('researcher', 'reviewer')]
        original = run(bundle, reviews=reviews)
        policy = DecisionPolicy(minimum_net_rr=1)
        attach_decisions(original, bundle, policy=policy)
        self.assertEqual(original['securities'][0]['trade_decision']['conclusion'], 'CONSIDER')
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder)/'history.sqlite3'
            record_observation(original, db)
            later = deepcopy(original)
            later.update(as_of='2026-09-09T16:00:00+09:00', snapshot_id='test-newer-invalidated')
            later['securities'][0]['technical'].update(latest_session='2026-09-09', latest_close=95, state='INVALIDATED')
            record_observation(later, db)
            original['ledger'] = record_observation(original, db)
            attach_decisions(original, bundle, policy=policy)
            result = original['securities'][0]['trade_decision']
            self.assertEqual(result['conclusion'], 'NO_TRADE')
            self.assertIn('LEDGER_INVALIDATED', result['reason_codes'])
            self.assertIn('LEDGER_STALE', result['reason_codes'])


if __name__ == '__main__':
    unittest.main()
