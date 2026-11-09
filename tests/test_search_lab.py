from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support_scout.contracts import DataError
from support_scout.decision_lab import run_lab
from support_scout.search_lab import freeze_selections, compare_saved, run_comparison, replay_comparison
from tests.test_decision_lab import fixture, SearchFixture, ReviewFixture


class SearchLabTests(unittest.TestCase):
    def saved_lab(self, root):
        b,p = fixture()
        return run_lab(b, plan=p, agents=True, providers=(SearchFixture(),ReviewFixture(),ReviewFixture()), output_root=root)

    def test_offline_replay_and_tampered_labels_refused_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            result,lab = self.saved_lab(Path(folder)/'lab')
            with patch('support_scout.providers.post_json', side_effect=AssertionError('network forbidden')), \
                    patch('support_scout.discovery.post_json', side_effect=AssertionError('network forbidden')):
                r,out = run_comparison(lab, output_root=Path(folder)/'search')
                self.assertEqual(replay_comparison(out)['status'], 'MATCH')
                self.assertEqual(r['model_calls'], 0)
                self.assertEqual(r['network_calls'], 0)
                self.assertFalse(r['formal_thresholds_changed'])
                r['rows'][0]['rule_outcome']['status'] = 'FITTED_WINNER'
                (out/'comparison.json').write_text(json.dumps(r), encoding='utf-8')
                with self.assertRaisesRegex(DataError, 'COMPARISON_REPLAY'):replay_comparison(out)

    def test_freeze_cannot_see_scored_labels_actions_or_peer_reviews(self):
        with tempfile.TemporaryDirectory() as folder:
            result,lab = self.saved_lab(folder)
            p = result['panels'][0]
            r = json.loads((lab/(p['id']+'_report.json')).read_text(encoding='utf-8'))
            original = freeze_selections([(p,r)])
            r['reviews'] = [{'future_return':9999}]
            r['securities'][0]['opportunity_decision']['action']='PROBE_BUY_REVIEW'
            r['future_prices'] = [999999]
            self.assertEqual(freeze_selections([(p,r)]), original)
            # Outcome changes affect scoring, never the frozen selections.
            changed = deepcopy(result['rows'])
            for x in changed:x['primary_comparator']['status']='TARGET'
            a = compare_saved([(p,r)], changed, original)
            self.assertEqual(original, freeze_selections([(p,r)]))
            self.assertEqual(a['rows'][0]['counterfactual_research'], 'NOT_RUN_NO_BUY_OR_RETURN_INFERENCE')

    def test_unselected_full_cap_is_not_fabricated_model_rank_or_desk_deferral(self):
        with tempfile.TemporaryDirectory() as folder:
            result,lab = self.saved_lab(folder)
            p=result['panels'][0];r=json.loads((lab/(p['id']+'_report.json')).read_text(encoding='utf-8'))
            # Fabricated extra case shares only test evidence; no live experiment used.
            original_symbol=r['securities'][0]['symbol'];extra=deepcopy(r['securities'][0]);extra['symbol']='9999.T'
            r['securities'].append(extra)
            c=deepcopy(r['discovery']['search']['search_packet']['core']['candidates'][0]);c['symbol']='9999.T'
            d=freeze_selections([(p,json.loads((lab/(p['id']+'_report.json')).read_text(encoding='utf-8')))])
            r['discovery']['search']['search_packet']['core']['candidates'].append(c)
            d[0]['orders']['legacy-v1'].append('9999.T');d[0]['orders']['support-evidence-v1-20261105'].append('9999.T')
            rows=deepcopy(result['rows'])
            for x in list(rows):
                x=deepcopy(x);x['symbol']='9999.T';x['selected_by_search']=False;x['research_status']='AWAITING_REVIEW';rows.append(x)
            a=compare_saved([(p,r)],rows,d)
            miss=next(x for x in a['rows'] if x['symbol']=='9999.T')
            self.assertEqual(miss['stage'],'SEARCH_NOT_SELECTED_CAP_FULL')
            self.assertIsNone(miss['unselected_model_rank'])
            self.assertIsNotNone(miss['attribution_limit'])
            r['discovery']['search']['selection']=[]
            self.assertEqual(compare_saved([(p,r)],rows,d)['rows'][-1]['stage'],'SEARCH_NOT_SELECTED_SPARE_CAPACITY')


if __name__ == '__main__':unittest.main()
