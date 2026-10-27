from decimal import Decimal
import unittest

from execution.settlement_balance import assess_settlement_balance


DUES = [
    dict(trade_date='2026-10-08', settlement_date='2026-10-13', net_twd='-6021'),
    dict(trade_date='2026-10-12', settlement_date='2026-10-14', net_twd='-224'),
]


def snapshot(checked_at, balance, evidence=None, dues=DUES):
    return dict(checked_at=checked_at, broker_one_account_balance_twd=str(balance),
                broker_settlement_dues=dues,
                settlement_balance_evidence=evidence or {})


class SettlementBalanceEvidenceTests(unittest.TestCase):
    def test_matching_balance_change_is_evidence_not_settlement_confirmation(self):
        prior = snapshot('2026-10-12T17:00:00+08:00', 30000)
        morning = snapshot('2026-10-13T09:20:00+08:00', 30000)
        evidence = assess_settlement_balance(prior, morning)
        self.assertEqual(evidence['2026-10-13']['status'], 'BALANCE_DIFFERENT')
        morning['settlement_balance_evidence'] = evidence
        after = snapshot('2026-10-13T10:30:00+08:00', 23979)
        evidence = assess_settlement_balance(morning, after)
        self.assertEqual(evidence['2026-10-13']['status'], 'BALANCE_MATCH')
        self.assertEqual(Decimal(evidence['2026-10-13']['observed_delta_twd']),
                         Decimal('-6021'))
        self.assertEqual(evidence['2026-10-13']['baseline_balance_twd'], '30000')

    def test_other_cash_activity_leaves_result_unresolved(self):
        prior = snapshot('2026-10-12T17:00:00+08:00', 30000)
        after = snapshot('2026-10-13T10:30:00+08:00', 24979)
        evidence = assess_settlement_balance(prior, after)
        self.assertEqual(evidence['2026-10-13']['status'], 'BALANCE_DIFFERENT')
        self.assertEqual(evidence['2026-10-13']['observed_delta_twd'], '-5021')

    def test_last_pre_due_balance_becomes_baseline(self):
        old = snapshot('2026-10-11T17:00:00+08:00', 31000)
        latest = snapshot('2026-10-12T17:00:00+08:00', 30000)
        evidence = assess_settlement_balance(old, latest)
        self.assertEqual(evidence['2026-10-13']['baseline_balance_twd'], '30000')
        self.assertEqual(evidence['2026-10-13']['status'], 'NO_BASELINE')

    def test_two_due_dates_across_gap_cannot_be_attributed(self):
        prior = snapshot('2026-10-12T17:00:00+08:00', 30000)
        after = snapshot('2026-10-14T17:00:00+08:00', 23755)
        evidence = assess_settlement_balance(prior, after)
        self.assertEqual(evidence['2026-10-13']['status'], 'MULTIPLE_DUE_DATES')
        self.assertEqual(evidence['2026-10-14']['status'], 'MULTIPLE_DUE_DATES')

    def test_missing_prior_balance_never_claims_match(self):
        prior = snapshot('2026-10-12T17:00:00+08:00', None)
        after = snapshot('2026-10-13T10:30:00+08:00', 23979)
        evidence = assess_settlement_balance(prior, after)
        self.assertEqual(evidence['2026-10-13']['status'], 'NO_BASELINE')

    def test_temporary_query_failure_preserves_baseline(self):
        prior = snapshot('2026-10-12T17:00:00+08:00', 30000)
        prior['settlement_balance_evidence'] = assess_settlement_balance({}, prior)
        failed = snapshot('2026-10-13T09:00:00+08:00', None, dues=[])
        failed['settlement_query_error'] = 'temporary error'
        preserved = assess_settlement_balance(prior, failed)
        self.assertEqual(preserved['2026-10-13']['baseline_balance_twd'], '30000')
        failed['settlement_balance_evidence'] = preserved
        recovered = snapshot('2026-10-13T10:30:00+08:00', 23979)
        evidence = assess_settlement_balance(failed, recovered)
        self.assertEqual(evidence['2026-10-13']['status'], 'BALANCE_MATCH')


if __name__ == '__main__':
    unittest.main()
