from __future__ import annotations

import unittest

from position_analyzer import validate


class BudgetWarningRepairTests(unittest.TestCase):
    def test_missing_budget_words_are_filled_from_checked_amount(self):
        candidate = dict(symbol='2345', last_price=10.0, quote_status='LIVE',
                         is_trial=False, max_buy_qty=10,
                         paper_position=dict(qty=0))
        decision = dict(symbol='2345', decision='BUY', confidence=0.7,
                        action_ratio=0, suggested_qty=1, reference_price=10.0,
                        suggested_price=10.0, reason='測試', data_quality='LIVE',
                        warnings=[])
        result = dict(market_view='測試', decisions=[decision])
        self.assertIs(validate(result, [candidate], 100000, minimum_buy_fee_twd=20), result)
        self.assertIn('預算', decision['warnings'][0])

    def test_numeric_budget_violation_still_fails(self):
        candidate = dict(symbol='2345', last_price=10.0, quote_status='LIVE',
                         is_trial=False, max_buy_qty=10,
                         paper_position=dict(qty=0))
        decision = dict(symbol='2345', decision='BUY', confidence=0.7,
                        action_ratio=0, suggested_qty=10, reference_price=10.0,
                        suggested_price=10.0, reason='測試', data_quality='LIVE',
                        warnings=[])
        with self.assertRaisesRegex(ValueError, 'AGGREGATE_BUY_COST_EXCEEDS_AVAILABLE_CASH'):
            validate(dict(market_view='測試', decisions=[decision]),
                     [candidate], 1000, minimum_buy_fee_twd=20)


if __name__ == '__main__':
    unittest.main()
