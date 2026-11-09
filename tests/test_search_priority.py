"""Research ordering must not change entry gates or borrow future labels."""
from copy import deepcopy
import unittest

from support_scout.contracts import DataError
from support_scout.discovery import make_search_packet, verify_search_packet
from support_scout.search_priority import LEGACY, SUPPORT_EVIDENCE, rank_candidates, support_evidence
from support_scout.communication import load_policy, search_wire
from tests.test_discovery import fixture


class SearchPriorityTests(unittest.TestCase):
    def setUp(self):
        self.report, self.universe = fixture()
        self.packet = make_search_packet(self.report, self.universe, ranking_version=LEGACY)
        self.cutoff = self.report['as_of']

    def supported(self):
        c = deepcopy(self.packet['core']['candidates'][-1])
        c['technical']['technical_candidate'] = False
        c['technical']['state'] = 'EXPIRED'  # Independent ATR episode, not H-002.
        c['patterns'] = [{'pattern_id': 'H-002', 'stage': 'SUPPORT_HELD', 'research_candidate': True,
            'metrics': {'distance_from_support_fraction': .04, 'volume_basis_comparable': False},
            'definition': {'parameters': {'max_distance_fraction': .06}},
            'support': {'anchor': 100, 'lower': 99, 'upper': 104},
            'required_evidence_ids': list(c['required_evidence_ids'])}]
        return c

    def test_same_pool_gates_and_evidence_baseline_is_retained(self):
        before = deepcopy(self.report)
        new = make_search_packet(self.report, self.universe)
        self.assertEqual(self.report, before)
        self.assertEqual({c['symbol'] for c in new['core']['candidates']},
            {c['symbol'] for c in self.packet['core']['candidates']})
        self.assertEqual(new['core']['ranking_version'], SUPPORT_EVIDENCE)
        self.assertNotEqual(new['packet_id'], self.packet['packet_id'])
        self.assertEqual(self.report['rules'], new['core']['rules'])
        # Legacy saved packets lacking a ranking marker still verify.
        from support_scout.contracts import digest
        old = deepcopy(self.packet);old['core'].pop('ranking_version')
        old['packet_id'] = digest({k:v for k,v in old.items() if k != 'packet_id'})
        verify_search_packet(old)

    def test_held_support_has_raw_evidence_priority_without_reviving_atr(self):
        c = self.supported();before = deepcopy(c)
        signal = support_evidence(c, self.cutoff)
        self.assertEqual(signal['grade'], 1)
        self.assertFalse(signal['signals'][0]['volume_verified'])
        self.assertEqual(c, before)
        self.assertEqual(c['technical']['state'], 'EXPIRED')

    def test_stale_labels_foreign_missing_or_future_evidence_and_extension_not_boosted(self):
        for change in ('stage', 'refs', 'future', 'distance', 'not_candidate'):
            c = self.supported();p = c['patterns'][0]
            if change == 'stage': p['stage'] = 'INVALIDATED'
            if change == 'refs': p['required_evidence_ids'] = ['foreign']
            if change == 'future':
                for e in c['evidence']:e['available_at'] = '2099-01-01T00:00:00+09:00'
            if change == 'distance': p['metrics']['distance_from_support_fraction'] = .061
            if change == 'not_candidate': p['research_candidate'] = False
            with self.subTest(change=change):self.assertEqual(support_evidence(c, self.cutoff)['grade'], 0)

    def test_confirmed_atr_cannot_use_an_old_unlinked_confirmation(self):
        c = self.supported();c['patterns'] = []
        c['technical'].update(technical_candidate=True, state='RULE_CONFIRMED', position='NEAR_SUPPORT', support={'evidence_ids': []})
        c['evidence'].append({'id':'old-confirmation', 'kind':'CONFIRMATION', 'available_at':self.cutoff})
        self.assertEqual(support_evidence(c, self.cutoff)['grade'], 0)
        c['technical']['support']['evidence_ids'] = ['old-confirmation']
        self.assertEqual(support_evidence(c, self.cutoff)['grade'], 3)

    def test_support_boost_is_deterministic_and_does_not_promote_over_formal_candidate(self):
        supported = self.supported()
        trigger = deepcopy(supported);trigger['symbol']='9999.T';trigger['patterns']=[{'pattern_id':'H-004','stage':'SHORT_RECLAIM_READY'}]
        confirmed = deepcopy(trigger);confirmed['symbol']='9998.T';confirmed['technical']['technical_candidate']=True
        ordered = rank_candidates([trigger,supported,confirmed], cutoff=self.cutoff)
        self.assertEqual([c['symbol'] for c in ordered], ['9998.T',supported['symbol'],'9999.T'])
        self.assertEqual(ordered,rank_candidates(list(reversed(ordered)), cutoff=self.cutoff))
        with self.assertRaises(DataError):rank_candidates([], version='fitted-to-winners', cutoff=self.cutoff)

    def test_new_wire_hints_use_only_request_bound_evidence_aliases(self):
        self.report['communication_policy'] = load_policy()
        p = make_search_packet(self.report, self.universe)
        view,mapping,_ = search_wire(p)
        self.assertEqual(view['ranking_version'], SUPPORT_EVIDENCE)
        for c in view['candidates']:
            for s in c.get('support_priority',view['support_priority_if_omitted'])['signals']:
                self.assertTrue(set(s['evidence_ids']) <= c['evidence'].keys())


if __name__ == '__main__':unittest.main()
