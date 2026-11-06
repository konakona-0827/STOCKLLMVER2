from copy import deepcopy
from decimal import Decimal
import unittest

from entry_screen import entry_screen
from execution.auto_advice_executor import LiveExecutionConfig
from token_budget import candidate_view
from trade_economics import evaluate_buy


class EntryScreenTests(unittest.TestCase):
    def setUp(self):
        self.account = dict(available_cash_cents=1000000, capital_limit_twd=10000)
        self.rules = LiveExecutionConfig(min_buy_fee_reserve_twd=Decimal('1'))
        self.c = dict(symbol='2330', asset_type='STOCK', last_price=100, bid=99.9, ask=100,
            quote_status='LIVE', is_trial=False, quote_timestamp='2026-11-02T10:00:00+08:00',
            max_buy_qty=99, eligible_strategies=['multi_day_trend'],
            paper_position=dict(qty=0), price_levels=[
                dict(id='previous_close', price=90, basis='2026-10-30'),
                dict(id='low_5d', price=89, basis='2026-10-26..2026-10-30'),
                dict(id='high_5d', price=100, basis='2026-10-26..2026-10-30')])

    def test_planned_pullback_can_be_viable_when_buying_at_ask_has_no_target(self):
        original = deepcopy(self.c)
        self.assertEqual(entry_screen(self.c, self.account, self.rules)['status'], 'NO_SCENARIO_PASSED')
        self.c['pricing_policy'] = 'PLANNED_LIMIT'
        result = entry_screen(self.c, self.account, self.rules)
        self.assertEqual(result['status'], 'SCENARIOS_AVAILABLE', result)
        for row in result['scenarios']:
            self.assertEqual(row['entry'], 90)
            self.assertEqual(row['target'], 100)
            self.assertLessEqual(row['budget_with_reserve_twd'], 3000)
            self.assertLessEqual(row['estimated_net_downside_twd'], 50)
            self.assertGreaterEqual(row['net_return'], .02)
            self.assertGreaterEqual(row['reward_risk_ratio'], 1.5)
            checked = evaluate_buy(self.c, qty=row['qty'], limit_price=row['entry'],
                target_price=row['target'], invalidation_price=row['invalidation'])
            self.assertEqual(row['estimated_net_upside_twd'], checked['estimated_net_upside_twd'])
        self.c.pop('pricing_policy')
        self.assertEqual(self.c, original)

    def test_minimum_commission_cannot_be_removed_to_make_small_positions_pass(self):
        self.c['pricing_policy'] = 'PLANNED_LIMIT'
        self.account['available_cash_cents'] = 100000
        self.rules = LiveExecutionConfig(min_buy_fee_reserve_twd=Decimal('20'))
        result = entry_screen(self.c, self.account, self.rules)
        self.assertEqual(result['status'], 'NO_SCENARIO_PASSED', result)

    def test_nearer_raw_daily_high_cannot_be_skipped_for_a_distant_target(self):
        self.c['pricing_policy'] = 'PLANNED_LIMIT'
        self.c['daily_bars'] = [dict(date='2026-10-30', high=91, complete=True, source='TWSE_DAILY')]
        self.assertEqual(entry_screen(self.c, self.account, self.rules)['status'], 'NO_SCENARIO_PASSED')
        self.c['daily_bars'][0]['date'] = '2026-11-03'
        self.assertEqual(entry_screen(self.c, self.account, self.rules)['status'], 'SCENARIOS_AVAILABLE')

    def test_stale_and_trial_quotes_and_losing_additions_do_not_produce_scenarios(self):
        self.c['pricing_policy'] = 'PLANNED_LIMIT'
        for change in (dict(quote_status='LAST_KNOWN'), dict(is_trial=True),
                       dict(paper_position=dict(qty=1), ai_cost_basis=dict(available=True, average_unit_cost_twd=101))):
            row = {**self.c, **change}
            self.assertFalse(entry_screen(row, self.account, self.rules)['scenarios'])

    def test_compact_view_keeps_risk_fields_and_can_retrieve_all_older_rows(self):
        self.c['daily_bars'] = [dict(date=f'2026-10-{d:02}', high=100, low=89) for d in range(1,23)]
        self.c['buy_plans'] = dict(available=True, lots=[dict(qty=1, plan=dict(invalidation_price=89))])
        original = deepcopy(self.c)
        summary = candidate_view(self.c, self.account, self.rules)
        full = candidate_view(self.c, self.account, self.rules, full_history=True)
        self.assertEqual(len(summary['daily_bars']['rows']), 5)
        self.assertEqual(len(full['daily_bars']['rows']), 22)
        self.assertTrue(summary['daily_bars_coverage']['truncated'])
        self.assertEqual(summary['buy_plans'], self.c['buy_plans'])
        levels = summary['price_levels']
        self.assertEqual([dict(zip(levels['columns'], row)) for row in levels['rows']], self.c['price_levels'])
        self.assertEqual(self.c, original)


if __name__ == '__main__':
    unittest.main()
