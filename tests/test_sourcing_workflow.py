"""End-to-end SEARCH handoff and shared-budget tests, with no network or real keys."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from support_scout.contracts import DataError, digest
from support_scout.decision import attach_decisions, DecisionPolicy
from support_scout.desk import run_desk, DeskConfig
from support_scout.discovery import SearchCompletion
from support_scout.japan_demo import demo_bundle
from support_scout.observation import observe, _cursor_commit, _cursor_read
from support_scout.pipeline import run
from support_scout.providers import Completion
from support_scout.universe import PROTOCOL, ORDERING


def fixture():
    bundle = demo_bundle()
    bundle.update(synthetic=False, observation_only=True, collection={'status': 'COMPLETE'})
    for item in bundle['securities']:
        item.update(observation_only=True, price_basis='raw_unverified', actions_reviewed=False,
            tradability_reviewed=False, volume_comparable=False, point_in_time=False)
    items = [{'symbol': s['symbol'], 'name': s['name']} for s in bundle['securities']]
    universe = {'protocol': PROTOCOL, 'ordering': ORDERING, 'universe_id': 'test-jpx-universe',
        'items': items, 'symbols': [s['symbol'] for s in items], 'ordered_items_digest': digest(items),
        'as_of': '2026-08-31', 'available_at': bundle['as_of'], 'sha256': 'fixture'}
    return bundle, universe


class Searcher:
    model, mode = 'test-search', 'test_transport'
    def __init__(self, indices=(0,), invalid=False):
        self.indices, self.invalid, self.calls = indices, invalid, []
    def complete(self, packet):
        self.calls.append(deepcopy(packet))
        selected = [{'candidate_id': packet['core']['candidates'][i]['candidate_id'],
            'evidence_ids': packet['core']['candidates'][i]['required_evidence_ids'],
            'rationale': '僅供測試：將同份快照交給獨立決策。'} for i in self.indices]
        if self.invalid:
            selected[0]['candidate_id'] = 'invented'
        return SearchCompletion({'selections': selected})


class Reviewer:
    model, mode = 'test-review', 'test_transport'
    def __init__(self):
        self.calls = []
    def complete(self, packet, role):
        self.calls.append((deepcopy(packet), role))
        return Completion({'verdict': 'RESEARCH', 'evidence_ids': packet['core']['required_confirmation_evidence'],
            'summary': '僅供測試：不能覆寫行情閘門。', 'risks': [], 'missing_data': []})


class SourcingWorkflowTests(unittest.TestCase):
    def execute(self, root, *, search=None, agents=True, calls=5, **overrides):
        bundle, universe = fixture()
        reviewer = Reviewer()
        options = dict(bundle=bundle, universe=universe, discover=True, agents=agents,
            search_provider=search or Searcher(), researcher=reviewer, reviewer=reviewer,
            max_calls=calls, max_packets=5, settings_path=root/'missing.json',
            output_root=root/'out', db_path=root/'state.sqlite3')
        options.update(overrides)
        report, target, code = observe(**options)
        return report, target, code, reviewer

    def test_selected_candidates_only_reach_independent_research_pair(self):
        with tempfile.TemporaryDirectory() as folder:
            search = Searcher((1, 0))
            report, target, code, reviewer = self.execute(Path(folder), search=search)
            result = report['discovery']['search']
            self.assertEqual(result['status'], 'COMPLETED')
            selected = [s['symbol'] for s in result['selection']]
            self.assertEqual(report['desk']['selected_symbols'], selected)
            self.assertEqual([r[0]['core']['symbol'] for r in reviewer.calls], [selected[0]]*2+[selected[1]]*2)
            self.assertEqual(report['discovery']['total_call_count'], 5)
            self.assertEqual(len(search.calls), 1)
            self.assertEqual(result['selection'][0]['pre_rank'], 2)
            self.assertEqual(result['selection'][0]['agent_order'], 1)
            self.assertTrue(all('discovery' not in p['core'] for p, _ in reviewer.calls))
            self.assertTrue((target/'search_audit.json').exists())
            saved = json.loads((target/'input.json').read_text(encoding='utf8'))
            replay = run(saved, reviews=report['reviews'])
            self.assertEqual(replay['snapshot_id'], report['snapshot_id'])
            self.assertEqual(replay['discovery']['search']['selection'], result['selection'])

    def test_failed_search_has_no_fallback_or_downstream_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            report, _, code, reviewer = self.execute(Path(folder), search=Searcher(invalid=True))
            self.assertEqual(report['discovery']['search']['status'], 'FAILED')
            self.assertEqual(reviewer.calls, [])
            self.assertNotIn('desk', report)
            self.assertEqual(report['discovery']['total_call_count'], 1)
            self.assertEqual(code, 3)

    def test_abstention_is_valid_and_does_not_run_decision_models(self):
        with tempfile.TemporaryDirectory() as folder:
            report, _, _, reviewer = self.execute(Path(folder), search=Searcher(()))
            self.assertEqual(report['discovery']['search']['status'], 'ABSTAIN')
            self.assertEqual(reviewer.calls, [])
            self.assertNotEqual(report['observation']['status'], 'DEGRADED')

    def test_numeric_only_never_claims_search_participation(self):
        with tempfile.TemporaryDirectory() as folder:
            search = Searcher()
            report, _, _, reviewer = self.execute(Path(folder), search=search, agents=False)
            self.assertEqual(report['discovery']['mode'], 'NUMERIC_ONLY')
            self.assertIsNone(report['discovery']['search'])
            self.assertEqual(search.calls, [])
            self.assertEqual(reviewer.calls, [])

    def test_total_budget_reserves_search_before_review_pairs(self):
        with tempfile.TemporaryDirectory() as folder:
            report, _, _, reviewer = self.execute(Path(folder), calls=4)
            self.assertEqual(report['discovery']['search']['search_packet']['core']['config']['max_selected'], 1)
            self.assertEqual(report['discovery']['total_call_count'], 3)
            self.assertEqual(len(reviewer.calls), 2)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(DataError, 'DISCOVERY_REQUIRES'):
                self.execute(Path(folder), calls=2)
        with tempfile.TemporaryDirectory() as folder:
            report, _, _, _ = self.execute(Path(folder), calls=100, max_packets=50)
            self.assertEqual(report['discovery']['search']['search_packet']['core']['config']['max_selected'], 40)

    def test_missing_search_configuration_never_uses_numeric_top_candidate(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {}, clear=True):
            report, _, code, reviewer = self.execute(Path(folder), search_provider=None)
            self.assertEqual(report['observation']['agent_status'], 'CONFIGURATION_REQUIRED')
            self.assertEqual(reviewer.calls, [])
            self.assertEqual(code, 3)

    def test_forged_selection_cannot_survive_rechecking(self):
        with tempfile.TemporaryDirectory() as folder:
            report, _, _, _ = self.execute(Path(folder))
            result = report['discovery']['search']
            result['selection'][0]['symbol'] = '6857.T'
            saved_input = json.loads((Path(report['observation']['report_directory'])/'input.json').read_text(encoding='utf8'))
            attach_decisions(report, saved_input)
            self.assertIn('SEARCH_INTEGRITY_FAILED', report['securities'][0]['trade_decision']['reason_codes'])

    def test_default_universe_cohort_rotates_after_committed_report(self):
        bundle, universe = fixture()
        requested = []
        def builder(symbols, cache_dir, **kwargs):
            requested.append(symbols)
            snapshot = deepcopy(bundle)
            snapshot['securities'] = [s for s in snapshot['securities'] if s['symbol'] in symbols]
            return snapshot
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for _ in range(2):
                observe(agents=False, universe=universe, builder=builder, scan_limit=2,
                    settings_path=root/'missing.json', output_root=root/'out', db_path=root/'state.sqlite3')
            self.assertEqual(requested, [universe['symbols'][:2], universe['symbols'][2:4]])
            self.assertEqual(_cursor_read(root/'discovery_cursor.json', universe['universe_id']), 4)

    def test_failed_publication_does_not_advance_cohort_cursor(self):
        bundle, universe = fixture()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch('support_scout.observation.publish_run', side_effect=OSError('disk failure')):
                with self.assertRaises(DataError):
                    observe(agents=False, universe=universe, builder=lambda *a, **kw: bundle, scan_limit=2,
                        settings_path=root/'missing.json', output_root=root/'out', db_path=root/'state.sqlite3')
            self.assertFalse((root/'discovery_cursor.json').exists())

    def test_cursor_compare_and_swap_prevents_skipped_batches(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'cursor.json'
            _cursor_commit(path, 'test', 0, 2)
            _cursor_commit(path, 'test', 0, 4)
            self.assertEqual(_cursor_read(path, 'test'), 2)

    def test_cursor_generation_rejects_old_writer_after_full_wrap(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'cursor.json'
            _cursor_commit(path, 'test', 0, 2, expected_revision=0)
            _cursor_commit(path, 'test', 2, 0, expected_revision=1)
            _cursor_commit(path, 'test', 0, 2, expected_revision=0)
            self.assertEqual(_cursor_read(path, 'test'), 0)
            self.assertEqual(json.loads(path.read_text())['revision'], 2)

    def test_direct_desk_entry_cannot_skip_failed_search_or_inject_other_symbols(self):
        with tempfile.TemporaryDirectory() as folder:
            report, target, _, _ = self.execute(Path(folder))
            saved = json.loads((target/'input.json').read_text(encoding='utf8'))
            reviewer = Reviewer()
            with self.assertRaisesRegex(DataError, 'DESK_SELECTION_NOT_APPROVED'):
                run_desk(saved, researcher=reviewer, reviewer=reviewer,
                    config=DeskConfig(1,2), selected_symbols=['9005.T'])
            saved['discovery']['search']['status'] = 'FAILED'
            saved['discovery']['search']['selection'] = []
            direct = run_desk(saved, researcher=reviewer, reviewer=reviewer, config=DeskConfig(1,2))
            self.assertEqual(direct['desk']['call_count'], 0)
            self.assertEqual(direct['desk']['status'], 'SKIPPED_DISCOVERY')
            self.assertEqual(reviewer.calls, [])

    def test_failed_dynamic_search_retries_same_cohort_next_run(self):
        bundle, universe = fixture()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reviewer = Reviewer()
            observe(agents=True, universe=universe, builder=lambda *a, **kw: bundle,
                search_provider=Searcher(invalid=True), researcher=reviewer, reviewer=reviewer,
                settings_path=root/'missing.json', output_root=root/'out', db_path=root/'state.sqlite3')
            self.assertFalse((root/'discovery_cursor.json').exists())

    def test_monthly_universe_update_continues_after_last_scanned_symbol(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'cursor.json'
            old = ['9001.T','9002.T','9003.T']
            _cursor_commit(path, 'september', 0, 2, symbols=old, universe_as_of='2026-09-30')
            new = ['1301.T',*old,'9004.T']
            self.assertEqual(_cursor_read(path, 'october', new), 3)
            _cursor_commit(path, 'october', 3, 4, symbols=new, universe_as_of='2026-10-31')
            _cursor_commit(path, 'september', 0, 1, symbols=old, universe_as_of='2026-09-30')
            self.assertEqual(json.loads(path.read_text())['universe_id'], 'october')


if __name__ == '__main__':
    unittest.main()
