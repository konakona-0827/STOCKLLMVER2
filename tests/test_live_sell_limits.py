import json
import unittest

from analysis_service import apply_live_cost_basis, apply_live_sell_limits
from position_analyzer import build_request, validate


class LiveSellLimitTests(unittest.TestCase):
    def candidate(self):
        return {'symbol': '1101', 'paper_position': {'qty': 100},
                'position': {'ai_managed_qty': 100}, 'quote_status': 'LIVE',
                'last_price': 26.5, 'is_trial': False}

    def sell_decision(self, quantity):
        return {'symbol': '1101', 'decision': 'SELL', 'confidence': 0.7,
                'action_ratio': quantity / 100, 'suggested_qty': quantity,
                'reference_price': 26.5, 'suggested_price': 26.5,
                'strategy_id': 'NONE', 'target_price': None,
                'invalidation_price': None, 'holding_days': None,
                'reason': '評估賣出', 'data_quality': 'LIVE', 'warnings': []}

    def test_holding_is_not_sellable_on_same_day(self):
        candidate = self.candidate()
        apply_live_sell_limits([candidate], {
            'inventory_checked': True,
            'inventory_comparison': [{'symbol': '1101', 'broker_cash_qty': 100,
                                      'broker_sellable_qty': 0}],
        })
        self.assertEqual(0, candidate['max_sell_qty'])
        result = validate({'market_view': '測試', 'decisions': [self.sell_decision(100)]},
                          [candidate])
        decision = result['decisions'][0]
        self.assertEqual('HOLD', decision['decision'])
        self.assertEqual(0, decision['suggested_qty'])
        self.assertIn('SELL_BLOCKED_BY_BROKER_SELLABLE_QTY', decision['warnings'])

    def test_sellable_quantity_caps_advice(self):
        candidate = self.candidate()
        apply_live_sell_limits([candidate], {
            'inventory_checked': True,
            'inventory_comparison': [{'symbol': '1101', 'broker_cash_qty': 100,
                                      'broker_sellable_qty': 30}],
        })
        self.assertEqual(30, candidate['max_sell_qty'])
        result = validate({'market_view': '測試', 'decisions': [self.sell_decision(20)]},
                          [candidate])
        self.assertEqual('SELL', result['decisions'][0]['decision'])

    def test_unverified_inventory_blocks_sell_advice(self):
        candidate = self.candidate()
        apply_live_sell_limits([candidate], {'inventory_checked': False,
                                             'inventory_comparison': []})
        self.assertEqual(0, candidate['max_sell_qty'])

    def test_live_request_tells_ai_the_sell_limit(self):
        candidate = self.candidate()
        candidate['max_sell_qty'] = 0
        request = build_request([candidate], {
            'source': 'CONFIGURED_CAP_REMAINING_WITH_BROKER_FUNDS_AND_RESERVE_CHECKS',
            'available_cash_twd': 0,
        }, strategy_prompt='測試策略')
        self.assertEqual('ADVICE_FOR_DETERMINISTIC_LIVE_EXECUTION',
                         json.loads(request['input'])['purpose'])
        self.assertIn('max_sell_qty', request['instructions'])

    def test_live_request_uses_reconstructed_cost_basis(self):
        candidate = self.candidate()
        apply_live_cost_basis([candidate], {'1101': {
            'quantity': 100, 'average_unit_cost_twd': '26.5', 'estimated': True,
        }})
        self.assertEqual(26.5, candidate['position']['ai_average_cost'])
        request = build_request([candidate], strategy_prompt='測試策略')
        sent = json.loads(request['input'])['analysis_set'][0]
        self.assertEqual('26.5', sent['ai_cost_basis']['average_unit_cost_twd'])
        self.assertIn('paper_position.cost_cents=null', request['instructions'])


if __name__ == '__main__':
    unittest.main()
